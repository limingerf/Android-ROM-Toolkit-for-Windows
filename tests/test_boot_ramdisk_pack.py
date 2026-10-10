"""Regressions for ramdisk packing in the boot repacker.

The Windows backend ships no `find`, so the old pipeline's `find . -mindepth 1`
was resolved by cmd.exe to the text-search utility, which rejects the option and
writes nothing; busybox cpio then produced a 124-byte trailer-only archive that
was renamed over the real ramdisk and published as a successful repack.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

from Scripts.Primary.Utils import V
from Scripts.ReMake import boot as remake_boot

NATIVE = V.toolchain.mode == "native"


class RamdiskEntryTests(unittest.TestCase):
    def test_entries_are_relative_posix_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bin").mkdir()
            (root / "bin" / "init").write_text("x", encoding="utf-8")
            (root / "init.rc").write_text("x", encoding="utf-8")
            self.assertEqual(
                remake_boot._ramdisk_entries(str(root)),
                ["bin", "bin/init", "init.rc"],
            )


class PackRamdiskTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ramdisk = self.root / "ramdisk"
        (self.ramdisk / "bin").mkdir(parents=True)
        (self.ramdisk / "bin" / "init").write_bytes(b"\x7fELF-fake-init-payload")
        (self.ramdisk / "init.rc").write_text("on boot\n", encoding="utf-8")
        self.output = self.root / "ramdisk-new.cpio"
        self.busybox = remake_boot._busybox_path()

    def test_missing_busybox_is_reported(self):
        original = remake_boot._busybox_path
        remake_boot._busybox_path = lambda: None
        self.addCleanup(setattr, remake_boot, "_busybox_path", original)
        self.assertFalse(remake_boot._pack_ramdisk(str(self.ramdisk), str(self.output)))
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(NATIVE, "the empty-tree guard is native-path specific")
    def test_empty_ramdisk_is_rejected(self):
        empty = self.root / "empty"
        empty.mkdir()
        self.assertFalse(remake_boot._pack_ramdisk(str(empty), str(self.output)))
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(NATIVE, "the direct busybox invocation is native-path specific")
    def test_real_ramdisk_is_archived_with_its_members(self):
        if not self.busybox:
            self.skipTest("bundled busybox is not available")
        self.assertTrue(remake_boot._pack_ramdisk(str(self.ramdisk), str(self.output)))
        self.assertGreater(self.output.stat().st_size, remake_boot.EMPTY_CPIO_SIZE)
        listing = subprocess.run(
            [self.busybox, "cpio", "-it", "-F", str(self.output)],
            capture_output=True, text=True,
        )
        self.assertEqual(listing.returncode, 0, listing.stderr)
        for member in ("bin", "bin/init", "init.rc"):
            self.assertIn(member, listing.stdout)

    @unittest.skipUnless(NATIVE, "the direct busybox invocation is native-path specific")
    def test_stale_archive_is_rebuilt_not_reused(self):
        if not self.busybox:
            self.skipTest("bundled busybox is not available")
        self.output.write_bytes(b"stale trailer")
        self.assertTrue(remake_boot._pack_ramdisk(str(self.ramdisk), str(self.output)))
        self.assertGreater(self.output.stat().st_size, remake_boot.EMPTY_CPIO_SIZE)


if __name__ == "__main__":
    unittest.main()
