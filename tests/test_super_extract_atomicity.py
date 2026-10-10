"""Regressions for the logical-partition (super) extraction helpers.

``_SparseRawCache.acquire`` returned a registered sparse->raw path without
checking that the file still existed, so the caller opened a missing file.

``LpUnpack._extract_partition`` wrote ``<name>.img`` in place, so an exception
inside the extent loop left a truncated image that later steps accepted as a
real partition.
"""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from Scripts.Primary import SuperTools


class SparseRawCacheTests(unittest.TestCase):
    def setUp(self):
        SuperTools._SparseRawCache._entries.clear()
        self.addCleanup(SuperTools._SparseRawCache._entries.clear)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _raw(self, name):
        path = self.root / name
        path.write_bytes(b"raw")
        return path

    def test_existing_entry_is_reused(self):
        first = self._raw("a.unsparse.img")
        self.assertEqual(SuperTools._SparseRawCache.acquire("key", str(first)), str(first))
        second = self._raw("b.unsparse.img")
        self.assertEqual(SuperTools._SparseRawCache.acquire("key", str(second)), str(first))

    def test_stale_entry_is_replaced_instead_of_returned(self):
        missing = self.root / "gone.unsparse.img"
        self.assertEqual(
            SuperTools._SparseRawCache.acquire("key", str(missing)), str(missing))
        fresh = self._raw("fresh.unsparse.img")
        self.assertEqual(
            SuperTools._SparseRawCache.acquire("key", str(fresh)), str(fresh))

    def test_cleanup_all_removes_files_and_registry(self):
        raw = self._raw("a.unsparse.img")
        SuperTools._SparseRawCache.acquire("key", str(raw))
        SuperTools._SparseRawCache.cleanup_all()
        self.assertFalse(raw.exists())
        self.assertEqual(SuperTools._SparseRawCache._entries, {})


class ExtractPartitionAtomicityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name)
        # Bypass __init__: it would open a real super image.
        self.unpack = SuperTools.LpUnpack.__new__(SuperTools.LpUnpack)
        self.unpack._out_dir = str(self.out)

    @staticmethod
    def _job(name="vendor"):
        return SuperTools.UnpackJob(
            name=name,
            geometry=SimpleNamespace(logical_block_size=4096),
            parts=[(0, 4096)],
        )

    def test_failed_extraction_leaves_no_image_behind(self):
        def explode(stream, offset, size, block_size):
            stream.write(b"x" * 128)
            raise SuperTools.LpUnpackError("boom")

        self.unpack._write_extent_to_file = explode
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SuperTools.LpUnpackError):
                self.unpack._extract_partition(self._job())
        self.assertEqual([path.name for path in self.out.iterdir()], [])

    def test_successful_extraction_publishes_the_image(self):
        def write(stream, offset, size, block_size):
            stream.write(b"y" * 256)

        self.unpack._write_extent_to_file = write
        with contextlib.redirect_stdout(io.StringIO()):
            self.unpack._extract_partition(self._job())
        self.assertEqual([path.name for path in self.out.iterdir()], ["vendor.img"])
        self.assertEqual((self.out / "vendor.img").read_bytes(), b"y" * 256)

    def test_failure_keeps_the_previous_image(self):
        previous = self.out / "vendor.img"
        previous.write_bytes(b"previous")

        def explode(stream, offset, size, block_size):
            raise SuperTools.LpUnpackError("boom")

        self.unpack._write_extent_to_file = explode
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SuperTools.LpUnpackError):
                self.unpack._extract_partition(self._job())
        self.assertEqual(previous.read_bytes(), b"previous")


if __name__ == "__main__":
    unittest.main()
