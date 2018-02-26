# Gbackup

***Note: This project is currently in the experiment phase. I'm trying out 
some ideas and maybe it'll turn into something useful, maybe not. But for 
now, this is not a working backup solution.***

Gbackup is a personal file backup solution that has the following key selling points:

* Low runtime memory usage, designed to run in the background
* Fast and efficient filesystem scans to discover changed files
* Content-addressable storage backend for deduplication and quick restores
  from any past snapshot (loosely based on Git's object format, 
  hence Gbackup)

These are the main design goals that are a priority for me:

* No special software needed on remote storage server (plan to target 
  Backblaze B2)
* Client side encryption (plan to incorporate libsodium)
* Runs as a daemon with built-in scheduler (no cobbling together wrapper 
  scripts and cron)
* Use asymmetric encryption to allow backup and prune operations 
  without the private key. Only restore operations will require
  the private key (this may change as the threat model is refined)
* Efficient pruning of old snapshots to recover space
* Continuous file monitoring with inotify (this goal was originally intended to 
  avoid having to do expensive filesystem scans, but I've since made the scanning 
  quite efficient, so this may not be necessary)

No other backup programs I've found quite met these criteria. Gbackup takes 
ideas from Borg, Duplicati, Restic, and others.

GBackup runs on Linux using Python 3.5.3 or newer. At the moment, 
compatability with any other platforms is coincidental.

These features are not a priority at the moment

* Multi-client support (multiple machines backing up to the same repository, 
with deduplication across all files. This would require repository locking 
and synchronizing of metadata, which isn't a problem I want to tackle right 
now)

## Architecture

### Terminology

Repository - The remote storage service where backup data is stored, 
    typically encrypted.
    
Backup set - The set of files on a local filesystem to be backed up. This is 
    defined by a single path to a root directory.
    
Snapshot - When a backup set is backed up, that forms a snapshot of all the 
    files in the backup set.

### Scan process

One of the fundamental aspects of any backup software is deciding which files
to back up and which files haven't changed since last backup. Some software, 
e.g. rsync, don't keep any local state and check which files have changed by 
comparing file attributes against a remote copy of the file. Some software 
keep a local database either in memory or on disk of file attributes and
compared the database values with the file. This is all to avoid transferring
more data across a perhaps slow network connection than is necessary, but 
with very large filesystems to back up, it's a necessary optimization.
 
GBackup can't read remote data, since that data may be encrypted with a 
private key that the backup process won't have access to. So our only option 
is to keep a local cache of file attributes of every file in the backup set. 
Early experiments used hierarchy of Python objects in memory. When the 
process started, the filesystem was traversed and the hierarchy of objects 
created, each object storing file attributes. Periodically, a simple 
recursive algorithm could traverse this tree in a post-order to iterate over 
all filesystem entries to either see if they've changed, or iterate over only
changed objects to back them up. Additionally, this architecture has a nice 
Python implementation using recursive generator functions and Python's `yield 
from` expressions. Each object's backup() function would yield objects to 
back up, and recurse using `yield from` into its child objects before backing
itself up.

This approach wasn't very fast and required quite a lot of memory to run. So 
I decided to experiment with the other extreme: store nothing in memory and
put all state into a local SQLite database. This turns out to have a lot of 
other advantages, in that I can do SQL queries to select and sort entries 
however I want, and gives more flexability in how I traverse the filesystem. 
But more importantly, it turns out to be very fast, with low memory usage. 
Scans are bounded mostly by the time to perform the lstat system call on 
every file in the backup set.

The current scan process is a multi-pass scan. On the first pass, it 
iterates over all objects in the local database (in arbitrary order) and 
performs an lstat to see if the entry has changed from what's in the database.
If it has changed, it's flagged as needing backup. For directories needing 
backup, a listdir call lists the children and any new entries are created for
new children. 

Subsequent passes perform the same operation on all newly created entries for
new files. Passes continue until there are no new files to scan.

This approach to scanning turns out to be very fast, especially for 
subsequent scans but even the inital scan. Memory usage remains low and if 
database writes are performed in a single SQLite transaction, IO is also kept
to a minimum. The reason scans are quick is it avoids problems with recursive 
tree traversals: each visit to a node would require a separate database query
or listdir call to get the list of children, which is more IO to perform. By 
scanning files in no particular order, every entry is streamed from the 
database in large batches and IO is kept to a minimum. The limiting factor is
having to perform all the lstat calls for every filesystem entry.

Note that a directory's mtime is updated by the creation or deletion of files
in the directory, so we can avoid listdir on unchanging directories. Also, if
the scan turns out to be CPU bound, it is easily parallelizable. However, 
raw speed is not the top priority, as consuming all of the CPU is not 
desirable for a program that runs in the background.

The backup process thus consists of iterating over all entries in the 
database that are flagged as needing backup, and backing them up.

### Storage Format

The storage repository is loosely based on Git's object store. With this, 
everything uploaded into the repository is an object, and objects are named 
after a hash of their contents. This has the advantage of inherent 
deduplication, as two objects with the same contents will only be stored once.

In this system, there are three kinds of objects: Tree objects, Inode 
objects, and Blob objects. They roughly correspond to their respective 
filesystem counterparts.

Tree objects store stat info about a directory, as well as a list of 
directory entries and the name of the objects for each entry. Directory 
entries can be other tree objects or inode objects.

Inode objects store stat info about a file, and a list of blob objects that 
make up the content of that file.

Blob objects store a blob of data.

Since each object is named with a hash of its contents, and objects reference
other objects by name, this forms a merkle tree. A root object's hash 
cryptographicaly verifies the entire object tree for a snapshot (much like Git).
If another snapshot is taken and only one file changed deep in the filesystem, 
then pushed to the repository are objects for the new file, as well as new 
objects for all parent directories up to the root.

Note that in this heirarchy of objects, objects may be referenced more than 
once�they may have more than one parent. A blob may be referenced by more 
than one inode (or several times in the same file), but also inode and tree 
objects may be referenced by more than one snapshot.

### Chunking

When backing up a file, the file's contents is split into chunks and each 
chunk is uploaded individually as its own blob. The algorithm for how to 
chunk the file will determine how good the deduplication is. Larger chunks 
mean it's less likely to match content elsewhere, while smaller chunks mean 
more uploads, more network overhead, and slower uploads.

Right now Gbackup uses a fixed size chunking algorithm: files are simply 
split every fixed number of bytes. This works well for most kinds of files 
found in a typical desktop user's home directory. Most files are going to be 
very small (so deduplication won't help much), or are going to be binary 
or compressed file formats that will be completely rewritten on change, and 
probably won't benefit much from deduplication at all.

Some backup systems (such as Borg) use variable sized chunks and a rolling 
hash to determine where to split the chunk boundaries. This has the advantage
of synchronizing chunk boundaries to the content. Consider a fixed size chunk
of 4MB. A large file that doesn't change will use the same set of blob objects 
every time. But if a single byte is inserted at the beginning of the 
file, all the data is pushed down by one byte. Now suddenly the chunks don't 
match previously uploaded ones since the previous chunks of data don't align 
with chunk boundaries. So the entire file is re-uploaded.

With a rolling hash over a window of bytes, as files are scanned, the 
decision to split a file is based on the hash of the data in the window. If 
one file is split at a particular location, and another file has the same 
byte sequence somewhere in it, then there will be a chunk split there, no 
matter where those bytes fall in the file.

I believe that for most kinds of files found in desktop users' home 
directories, fixed size chunking is sufficient. Most large binary formats 
avoid inserting bytes because that would involve copying large amounts of 
data to other sections of the file. Applications managing large data formats 
will probably have a smarter format that is more friendly to fixed size chunk
deduplication.

Some examples of files that perform poorly on fixed size chunking:

* virtual machine images, which may have lots of duplicate data throughout 
but not necessarily aligned to chunk boundaries.
* SQL database dumps. Each database dump will contain lots of identical data,
 but not necessarily in the same places within the file.
* Video files for video editing. Changes in one section of a video 
may change the alignment of the rendered video but content in other sections 
stays the same.

My conclusion is that implementing a rolling hash would help for some 
situations, but not for the most common cases, so I'm just implementing fixed
size chunking for now. Changing the chunking algorithm should be easy when it
becomes necessary.

Also, taking a hint from Backblaze, files below about 30MB in size [1] are 
probably not worth chunking at all, as the sorts of files that would benefit 
from a lot of deduplication are mostly huge files (database dumps, VM images,
etc). 

[1] 30MB is the threshold Backblaze uses, below which files aren't chunked.
https://help.backblaze.com/hc/en-us/articles/217666728-How-does-Backblaze-handle-large-files-

I've also noticed that an overwhelming 97% of files in my own home 
directory are less than 1 MB, out of about a million files. Such tiny files 
won't benefit much from deduplication or rolling hashes, but will benefit 
much more from pack files, where many objects are uploaded in a single pack. So 
pack files are a higher priority than rolling hashes.


### Object Cache

In the local database, a cache of objects is kept in a table. This table 
helps keep track of objects that have been uploaded to the remote repository,
saving network requests. This also lets us avoid uploading a file even if the
scanning process thinks a file changed when it hasn't. The file will be split
into chunks, the chunks hashed, and then the hash looked up in the database.
If the object already exists in the database, then it's assumed to have been 
uploaded to the repository already.

The Object cache also keeps track of relationships between objects. This is 
used when removing an old snapshot. When a snapshot is removed, the 
objects aren't immediately deleted, since they may be referenced by other 
snapshots. Instead, a garbage collection routine is used to traverse the 
object tree starting at each root, and calculate a set of unreachable objects.
Those objects are then deleted from the local cache and the remote repository.

### Pack files

### Backup process

### Encryption

