import tempfile
import pathlib
import logging
import stat
import os
import unittest
import hashlib

from django.db import connection

from backathon import scan, backup, models, restore
from .base import TestBase

class AssertionHandler(logging.Handler):
    """A logging handler that will raise an AssertionError if any warnings or
    errors are emitted

    Use this in tests by attaching it to a logger to make sure no warnings are
    emitted by that logger
    """
    def emit(self, record):
        raise AssertionError(self.format(record))

class TestRestore(TestBase):
    """Tests restore functionality, and some other end-to-end scan and backup
    functionality

    """
    # Set by subclasses that test encryption
    key = None

    def setUp(self):
        super().setUp()
        self.restoredir = self.stack.enter_context(
            tempfile.TemporaryDirectory()
        )

        self.handler = AssertionHandler()
        self.handler.setLevel(logging.WARNING)
        logging.getLogger("backathon.restore").addHandler(self.handler)

    def tearDown(self):
        logging.getLogger("backathon.restore").removeHandler(self.handler)
        super().tearDown()

    def assert_restored_file(self, path, contents):
        fullpath = pathlib.Path(self.restoredir, path)
        self.assertEqual(
            contents,
            fullpath.read_text(),
        )

    def test_simple_restore(self):
        self.create_file("file1", "contents1")
        self.create_file("dir/file2", "contents2")
        scan.scan()
        backup.backup()

        ss = models.Snapshot.objects.get()

        restore.restore_item(ss.root, self.restoredir, self.key)

        self.assert_restored_file("file1", "contents1")
        self.assert_restored_file("dir/file2", "contents2")

    def test_restore_mode(self):
        file_a = self.create_file("file1", "contents")
        file_a.chmod(0o777)

        scan.scan()
        backup.backup()
        ss = models.Snapshot.objects.get()
        restore.restore_item(ss.root, self.restoredir, self.key)

        file_b = pathlib.Path(self.restoredir, "file1")
        stat_result = file_b.stat()
        self.assertEqual(
            0o777,
            stat.S_IMODE(stat_result.st_mode),
        )

    def test_restore_uid_gid(self):
        file_a = self.create_file("file1", "contents")
        try:
            os.chown(file_a, 1, 1)
        except PermissionError:
            raise unittest.SkipTest("Process doesn't have chown permission")

        scan.scan()
        backup.backup()
        ss = models.Snapshot.objects.get()
        restore.restore_item(ss.root, self.restoredir, self.key)

        file_b = pathlib.Path(self.restoredir, "file1")
        stat_result = file_b.stat()
        self.assertEqual(
            1,
            stat_result.st_uid
        )
        self.assertEqual(
            1,
            stat_result.st_gid
        )

    def test_restore_time(self):
        file_a = self.create_file("file1", "contents")
        os.utime(file_a, ns=(123456789, 987654321))

        scan.scan()
        backup.backup()
        ss = models.Snapshot.objects.get()
        restore.restore_item(ss.root, self.restoredir, self.key)

        file_b = pathlib.Path(self.restoredir, "file1")

        stat_result = file_b.stat()
        self.assertEqual(
            123456789,
            stat_result.st_atime_ns,
        )
        self.assertEqual(
            987654321,
            stat_result.st_mtime_ns,
        )

    def test_restore_time_dir(self):
        dir_a = pathlib.Path(self.backupdir, "dir1")
        dir_a.mkdir()
        os.utime(dir_a, ns=(123456789, 987654321))

        scan.scan()
        backup.backup()
        ss = models.Snapshot.objects.get()

        # The directory atime gets reset before we back it up, so just check
        # that whatever value it had when it was backed up, that's what gets
        # restored.
        tree = ss.root.children.get()
        info = list(models.Object.unpack_payload(tree.payload))[1]
        atime = info['atime']

        restore.restore_item(ss.root, self.restoredir, self.key)

        dir1 = pathlib.Path(self.restoredir, "dir1")

        stat_result = dir1.stat()
        self.assertEqual(
            987654321,
            stat_result.st_mtime_ns,
        )
        self.assertEqual(
            atime,
            stat_result.st_atime_ns,
        )

    def test_restore_multiple_revisions(self):
        self.create_file("file", "contents A")

        scan.scan()
        backup.backup()

        self.create_file("file", "new contents")

        scan.scan()
        backup.backup()

        snapshots = list(models.Snapshot.objects.order_by("date"))

        self.assertEqual(2, len(snapshots))
        self.assertEqual(
            6,
            models.Object.objects.count()
        )

        restoredir = pathlib.Path(self.restoredir)
        restore.restore_item(snapshots[0].root, restoredir/"ss1", self.key)
        restore.restore_item(snapshots[1].root, restoredir/"ss2", self.key)

        file1 = restoredir / "ss1" / "file"
        file2 = restoredir / "ss2" / "file"

        self.assertEqual(
            "contents A",
            file1.read_text()
        )
        self.assertEqual(
            "new contents",
            file2.read_text()
        )

    def test_restore_single_file(self):
        self.create_file("file", "contents")

        scan.scan()
        backup.backup()

        root = models.Snapshot.objects.get().root

        # Should just be one child
        inode = root.children.get()

        filename = pathlib.Path(self.restoredir, "my_file")
        restore.restore_item(inode, filename, self.key)

        self.assertEqual(
            "contents",
            filename.read_text()
        )

    def test_restore_invalid_utf8_filename(self):
        name = os.fsdecode(b"\xFF\xFFHello\xFF\xFF")

        self.assertRaises(
            UnicodeEncodeError,
            name.encode,
            "utf-8"
        )

        self.create_file(name, "contents")

        scan.scan()
        backup.backup()
        ss = models.Snapshot.objects.get()

        restore.restore_item(ss.root, self.restoredir, self.key)

        #self.assert_restored_file(name, "contents")

    def test_calculate_children(self):
        """Checks that the Object.calculate_children() works as expected

        Doesn't actually call restore()
        """
        self.create_file("dir1/file1", "contents")
        self.create_file("dir1/file2", "asdf")
        self.create_file("dir2/file3", "aoeu")
        self.create_file("dir2/file4", "zzzz")

        scan.scan()
        backup.backup()

        self.assertEqual(
            11,
            models.Object.objects.count()
        )

        # Make sure the table is consistent, as the unit tests are run in a
        # transaction and so foreign key constraints are not enforced
        c = connection.cursor()
        c.execute("PRAGMA foreign_key_check")
        self.assertEqual(
            0,
            len(list(c))
        )

        for obj in models.Object.objects.all():
            self.assertSetEqual(
                set(b.hex() for b in obj.calculate_children()),
                {c.objid.hex() for c in obj.children.all()},
                "Object {}'s children don't match".format(obj)
            )

    def test_restore_large_file(self):
        """This file should take more than one block to save, so it tests
        routines that must operate on multiple blocks.

        """
        infile = self.create_file("bigfile", "")
        block = b"\0"*1024*1024
        h = hashlib.md5()

        with infile.open("wb") as f:
            for _ in range(50):
                h.update(block)
                f.write(block)

        scan.scan()
        backup.backup()
        ss = models.Snapshot.objects.get()
        restore.restore_item(ss.root, self.restoredir, self.key)

        outfile = pathlib.Path(self.restoredir, "bigfile")
        h2 = hashlib.md5()
        with outfile.open("rb") as f:
            while True:
                a = f.read(64*2**10)
                if not a:
                    break
                h2.update(a)
        self.assertEqual(
            h.hexdigest(),
            h2.hexdigest()
        )

class TestRestoreWithCompression(TestRestore):
    def setUp(self):
        super().setUp()
        models.Setting.set("COMPRESSION", "zlib")

class TestRestoreWithEncryption(TestRestore):
    def setUp(self):
        super().setUp()

        import nacl.public
        self.key = nacl.public.PrivateKey.generate()
        models.Setting.set("ENCRYPTION", "nacl")
        models.Setting.set("PUBKEY", bytes(self.key.public_key).hex())

    def test_not_plaintext(self):
        """Tests that the plaintext of a file doesn't appear in the object
        payload on disk"""
        self.create_file("secret_file", "super secret contents")

        scan.scan()
        backup.backup()

        ss = models.Snapshot.objects.get()
        tree = ss.root
        inode = tree.children.get()
        blob = inode.children.get()
        self.assertIs(None, blob.payload)

        path = pathlib.Path(self.datadir, "objects",
                            blob.objid.hex()[:3],
                            blob.objid.hex())
        self.assertTrue(path.exists())
        contents = path.read_bytes()
        self.assertFalse(
            b"super secret contents" in contents
        )

class TestRestoreEncryptionAndCompression(TestRestoreWithCompression,
                                          TestRestoreWithEncryption):
    pass