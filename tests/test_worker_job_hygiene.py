"""Regressions for job hygiene, image sizing and convert naming in the worker.

Three defects are covered here:

* "保留原始尺寸" sized a rebuild from ``stat().st_size`` of a possibly sparse
  INPUT image, i.e. from the container length instead of the logical size.
* The publish transaction directory was created in the project root, and a
  leftover ``.art-backup-*`` made ``ProjectLayout.state()`` report the project
  as unsupported forever.
* ``_convert`` named its output from ``Path.stem``, so ``system_a.sparse.img``
  became ``system_a.sparse.raw.img`` and later resolved to a bogus partition.
"""
import os
import struct
import tempfile
import time
import unittest
from pathlib import Path

from Scripts.Primary.WorkSpace import ProjectLayout
from Scripts.worker import (
    _convert,
    _logical_image_size,
    _publish,
    _sweep_stale_transactions,
)

SPARSE_MAGIC = 0xED26FF3A
CHUNK_RAW = 0xCAC1
CHUNK_DONT_CARE = 0xCAC3


def _sparse_header(block_size, total_blocks, total_chunks):
    return struct.pack(
        "<IHHHHIIII", SPARSE_MAGIC, 1, 0, 28, 12, block_size, total_blocks, total_chunks, 0
    )


def _chunk_header(kind, blocks, total_size):
    return struct.pack("<HHI", kind, 0, blocks) + struct.pack("<I", total_size)


def write_sparse_with_data(path, block_size=4096, blocks=1):
    """A one-chunk sparse image that carries real data."""
    payload = bytes(range(256)) * (block_size * blocks // 256)
    path.write_bytes(
        _sparse_header(block_size, blocks, 1)
        + _chunk_header(CHUNK_RAW, blocks, 12 + len(payload))
        + payload
    )


def write_sparse_dont_care(path, block_size=4096, blocks=2):
    """A tiny sparse container whose logical size is much larger."""
    path.write_bytes(
        _sparse_header(block_size, blocks, 1) + _chunk_header(CHUNK_DONT_CARE, blocks, 12)
    )


class LogicalImageSizeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_raw_image_uses_its_file_size(self):
        raw = self.root / "system.img"
        raw.write_bytes(b"x" * 1234)
        self.assertEqual(_logical_image_size(raw), 1234)

    def test_sparse_image_uses_the_logical_size_not_the_container(self):
        sparse = self.root / "system_a.sparse.img"
        write_sparse_dont_care(sparse, block_size=4096, blocks=2)
        container = sparse.stat().st_size
        self.assertLess(container, 8192)  # a 40-byte container ...
        self.assertEqual(_logical_image_size(sparse), 8192)  # ... for 8 KiB


class ConvertNamingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.layout = ProjectLayout(self.root / "proj").initialize()
        self.stage = ProjectLayout(self.root / "stage").initialize()

    def test_sparse_suffix_does_not_compound(self):
        source = self.layout.input_dir / "system_a.sparse.img"
        write_sparse_with_data(source)
        result = _convert(None, self.layout, self.stage, {"source": str(source), "target": "raw"})
        published = Path(result["outputs"][0])
        self.assertEqual(published.name, "system_a.raw.img")
        self.assertTrue(published.is_file())
        self.assertEqual(published.stat().st_size, 4096)

    def test_second_conversion_reuses_the_same_name(self):
        first = self.layout.input_dir / "system_a.sparse.img"
        write_sparse_with_data(first)
        _convert(None, self.layout, self.stage, {"source": str(first), "target": "raw"})
        raw = self.layout.out_dir / "system_a.raw.img"
        self.assertTrue(raw.is_file())
        result = _convert(None, self.layout, self.stage, {"source": str(raw), "target": "sparse"})
        self.assertEqual(Path(result["outputs"][0]).name, "system_a.sparse.img")


class PublishTransactionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.layout = ProjectLayout(self.root / "proj").initialize()
        self.stage = ProjectLayout(self.root / "stage").initialize()

    def _leftovers(self):
        found = []
        for base in (self.layout.project_dir, self.layout.workspace_dir, self.layout.out_dir):
            found += [p.name for p in base.iterdir() if p.name.startswith(".art-backup-")]
        return found

    def test_transaction_directory_never_lands_in_the_project_root(self):
        staged = self.stage.out_dir / "system.img"
        staged.write_bytes(b"new")
        destination = self.layout.out_dir / "system.img"
        _publish([(staged, destination)], replace=True, workspace=self.layout.workspace_dir)
        self.assertEqual(destination.read_bytes(), b"new")
        self.assertEqual(self._leftovers(), [])

    def test_failed_publish_restores_the_old_artifact(self):
        destination = self.layout.out_dir / "system.img"
        destination.write_bytes(b"old")
        staged = self.stage.out_dir / "system.img"
        staged.write_bytes(b"new")
        blocked = self.stage.out_dir / "blocked"
        blocked.write_bytes(b"i am a file, not a directory")
        with self.assertRaises(OSError):
            _publish(
                [(staged, destination), (staged, blocked / "system.img")],
                replace=True,
                workspace=self.layout.workspace_dir,
            )
        self.assertEqual(destination.read_bytes(), b"old")
        self.assertEqual(self._leftovers(), [])

    def test_project_with_a_stale_transaction_directory_is_still_supported(self):
        (self.layout.project_dir / ".art-backup-stale").mkdir()
        self.assertEqual(self.layout.state(), "new")

    def test_a_visible_extra_directory_still_means_unsupported(self):
        (self.layout.project_dir / "legacy").mkdir()
        self.assertEqual(self.layout.state(), "unsupported")


class StaleTransactionSweepTests(unittest.TestCase):
    def test_only_abandoned_directories_are_removed(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        old = root / ".art-job-abandoned"
        old.mkdir()
        (old / "system.img").write_bytes(b"x" * 32)
        fresh = root / ".art-job-live"
        fresh.mkdir()
        keep = root / "system"
        keep.mkdir()
        ancient = time.time() - 48 * 3600
        os.utime(old, (ancient, ancient))
        _sweep_stale_transactions(root)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.is_dir())
        self.assertTrue(keep.is_dir())


if __name__ == "__main__":
    unittest.main()
