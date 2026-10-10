"""Regressions for the extraction-path defects found in the read-only audit.

Covered here:

* ``extract_super`` dropped dispatch failures and then deleted the only copy of
  the affected images while still reporting success.
* ``decompress_img`` returned ``None`` for the deliberate dsp/exaid/cust skip,
  which the worker reads as a hard failure and aborts the whole job with.
* A DAT file whose name is not a safe component aborted the entire batch.
* The parallel DAT workers always reported success because the extractors
  swallowed their own errors.
* An interrupted fragment merge left a partial file that made every retry fail.
* EROFS re-extraction over an existing tree silently mixed two images.
"""
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from Scripts.Extract import dat_br as dat_br_mod
from Scripts.Extract import erofs as erofs_mod
from Scripts.Extract import super as super_mod
from Scripts.Extract.image import decompress_img
from Scripts.Primary.Utils import V
from Scripts.Primary.WorkSpace import ProjectLayout

SPARSE_MAGIC = 0xED26FF3A


def write_sparse(path, block_size=4096, blocks=1):
    header = struct.pack("<IHHHHIIII", SPARSE_MAGIC, 1, 0, 28, 12, block_size, blocks, 1, 0)
    chunk = struct.pack("<HHI", 0xCAC3, 0, blocks) + struct.pack("<I", 12)
    path.write_bytes(header + chunk)


class _BoundWorkspace(unittest.TestCase):
    """Bind V.layout/V.workspace/V.config the way the worker does."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.layout = ProjectLayout(self.root / "proj").initialize()
        self.workspace = self.layout.workspace_dir
        saved = {name: getattr(V, name, None) for name in ("layout", "workspace", "config", "JM")}
        V.layout = self.layout
        V.workspace = str(self.workspace) + os.sep
        V.config = str(self.layout.config_dir) + os.sep
        V.JM = True

        def restore():
            for name, value in saved.items():
                if value is None:
                    try:
                        delattr(V, name)
                    except AttributeError:
                        pass
                else:
                    setattr(V, name, value)

        self.addCleanup(restore)


class SuperDispatchTests(_BoundWorkspace):
    def _super_dir(self):
        directory = self.workspace / "super"
        directory.mkdir(exist_ok=True)
        (directory / "system.img").write_bytes(b"raw")
        return str(directory) + os.sep

    def test_failed_dispatch_keeps_the_images_and_reports_failure(self):
        directory = self._super_dir()
        with mock.patch.object(super_mod, "unpack", lambda *a, **k: None), \
                mock.patch.object(super_mod, "_super_images_to_process",
                                  lambda d: iter([(os.path.join(d, "system.img"), "system")])), \
                mock.patch("Scripts.Extract.image.decompress_img", lambda *a, **k: False):
            handled = super_mod.extract_super("super.img", "super")
        self.assertFalse(handled)
        self.assertTrue(Path(directory).is_dir(), "the only copy must be kept")

    def test_successful_dispatch_removes_the_intermediate_directory(self):
        directory = self._super_dir()
        with mock.patch.object(super_mod, "unpack", lambda *a, **k: None), \
                mock.patch.object(super_mod, "_super_images_to_process",
                                  lambda d: iter([(os.path.join(d, "system.img"), "system")])), \
                mock.patch("Scripts.Extract.image.decompress_img", lambda *a, **k: True):
            handled = super_mod.extract_super("super.img", "super")
        self.assertTrue(handled)
        self.assertFalse(Path(directory).is_dir())


class SkippedImageTests(_BoundWorkspace):
    def test_deliberate_skip_is_not_reported_as_a_failure(self):
        skipped = self.root / "dsp.img"
        write_sparse(skipped)
        self.assertTrue(decompress_img(str(skipped), str(self.workspace / "dsp")))

    def test_unsupported_image_is_still_a_failure(self):
        unknown = self.root / "mystery.img"
        unknown.write_bytes(b"not an image at all")
        self.assertFalse(decompress_img(str(unknown), str(self.workspace / "mystery")))


class DatPartitionListTests(_BoundWorkspace):
    def test_unsafe_names_are_skipped_instead_of_aborting_the_batch(self):
        good = self.root / "system.new.dat"
        good.write_bytes(b"x")
        (self.root / "system.transfer.list").write_text("4\n1\n0\n0\n", encoding="utf-8")
        unsafe = self.root / "system (1).new.dat"
        unsafe.write_bytes(b"x")
        (self.root / "system (1).transfer.list").write_text("4\n1\n0\n0\n", encoding="utf-8")
        items = dat_br_mod._list_dat_partitions([str(good), str(unsafe)])
        self.assertEqual([item["partition"] for item in items], ["system"])

    def test_extractor_failure_is_reported_as_failure(self):
        item = {"partition": "system", "path": "system.new.dat", "transfer": "system.transfer.list"}
        with mock.patch.object(dat_br_mod, "decompress_dat", lambda *a, **k: False):
            result = dat_br_mod._decompress_single_partition(item, 3)
        self.assertFalse(result["success"])
        self.assertIsNotNone(result["error"])

    def test_extractor_success_is_reported_as_success(self):
        item = {"partition": "system", "path": "system.new.dat", "transfer": "system.transfer.list"}
        with mock.patch.object(dat_br_mod, "decompress_dat", lambda *a, **k: True):
            result = dat_br_mod._decompress_single_partition(item, 3)
        self.assertTrue(result["success"])


class FragmentMergeTests(_BoundWorkspace):
    def test_interrupted_merge_leaves_no_partial_file(self):
        source = self.root / "system.new.dat"
        source.write_bytes(b"payload")
        fragment = self.root / "system.new.dat.1"
        fragment.write_bytes(b"more")
        combined = self.workspace / source.name
        with mock.patch.object(dat_br_mod.shutil, "copyfileobj",
                               side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                dat_br_mod._combine_fragments(str(source))
        self.assertFalse(combined.exists(), "a retry must not fail with 'File exists'")


class ErofsCleanTreeTests(_BoundWorkspace):
    def test_stale_tree_is_cleared_before_extraction(self):
        source = self.root / "system.img"
        write_sparse(source)
        destination = self.workspace / "system"
        destination.mkdir()
        (destination / "stale-from-an-older-image.txt").write_text("old", encoding="utf-8")
        observed = {}

        def fake_call(argv, *args, **kwargs):
            observed["stale_present"] = (destination / "stale-from-an-older-image.txt").exists()
            return 0

        with mock.patch.object(erofs_mod, "call", fake_call), \
                mock.patch.object(erofs_mod, "_normalize_erofs_metadata", lambda *a: True), \
                mock.patch.object(erofs_mod, "_commit_extracted_partition", lambda *a, **k: True):
            handled = erofs_mod.extract_erofs(str(source), "system", str(destination))
        self.assertTrue(handled)
        self.assertFalse(observed["stale_present"])


if __name__ == "__main__":
    unittest.main()
