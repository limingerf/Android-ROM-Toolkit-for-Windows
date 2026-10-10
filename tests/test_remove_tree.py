"""Regressions for removing trees that live beyond MAX_PATH.

``shutil.rmtree`` resolves the path itself, so a deep extraction tree below a
long project root could not be removed on Windows.  The same blindness made
``Path.exists()`` report False for the existing EXT4 output directory, so the
removal was skipped and the following ``mkdir`` failed with WinError 3.
"""
import os
import tempfile
import unittest
from pathlib import Path

from Scripts.Extract import ext4
from Scripts.Primary import Utils

SEGMENT = "d" * 60


def deep_path(root, levels=6):
    return os.path.join(str(root), *([SEGMENT] * levels))


class RemoveTreeTests(unittest.TestCase):
    def setUp(self):
        # TemporaryDirectory.cleanup() cannot remove ancestors that are beyond
        # MAX_PATH themselves, so this test owns its root.
        self.root = tempfile.mkdtemp()
        self.addCleanup(Utils.remove_tree, self.root, True)

    def test_deep_tree_is_removed(self):
        deep = deep_path(self.root)
        target = Path(Utils.extended_path(deep))
        target.mkdir(parents=True)
        (target / "marker.bin").write_bytes(b"x")
        Utils.remove_tree(deep)
        self.assertFalse(os.path.exists(Utils.extended_path(deep)))

    def test_regular_tree_is_removed(self):
        target = Path(self.root) / "tree" / "sub"
        target.mkdir(parents=True)
        (target / "a.bin").write_bytes(b"x")
        Utils.remove_tree(Path(self.root) / "tree")
        self.assertFalse((Path(self.root) / "tree").exists())

    def test_ignore_errors_accepts_a_missing_path(self):
        Utils.remove_tree(Path(self.root) / "absent", True)

    def test_missing_path_raises_without_ignore_errors(self):
        with self.assertRaises(FileNotFoundError):
            Utils.remove_tree(Path(self.root) / "absent")

    def test_rmdire_still_removes_a_deep_tree(self):
        deep = deep_path(self.root)
        Path(Utils.extended_path(deep)).mkdir(parents=True)
        Utils.rmdire(deep)
        self.assertFalse(os.path.exists(Utils.extended_path(deep)))


class PreparePartitionOutputTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(Utils.remove_tree, self.root, True)

    def test_plain_directory_is_recreated_empty(self):
        destination = Path(self.root) / "system"
        (destination / "app").mkdir(parents=True)
        (destination / "app" / "stale.bin").write_bytes(b"x")
        output_dir, config_dir = ext4._prepare_partition_output("system", destination)
        self.assertTrue(output_dir.is_dir())
        self.assertFalse((output_dir / "app").exists())
        self.assertTrue(config_dir.is_dir())

    def test_deep_existing_output_is_replaced(self):
        # This is the regression: the old code saw the deep tree as missing,
        # skipped the removal and failed in mkdir with WinError 3.
        deep = deep_path(self.root)
        target = Path(Utils.extended_path(deep))
        target.mkdir(parents=True)
        (target / "stale.bin").write_bytes(b"x")
        self.assertFalse(Path(deep).exists())  # invisible without the prefix
        output_dir, _ = ext4._prepare_partition_output("system", deep)
        self.assertTrue(os.path.isdir(Utils.extended_path(deep)))
        self.assertFalse(os.path.exists(Utils.extended_path(os.path.join(deep, "stale.bin"))))
        self.assertEqual(str(output_dir), deep)

    def test_file_in_the_way_is_rejected(self):
        destination = Path(self.root) / "system"
        destination.write_bytes(b"not a directory")
        with self.assertRaises(ext4.ImageExtractionError):
            ext4._prepare_partition_output("system", destination)

    def test_unsafe_partition_name_is_rejected(self):
        with self.assertRaises(ext4.ImageExtractionError):
            ext4._prepare_partition_output("../escape", Path(self.root) / "x")


if __name__ == "__main__":
    unittest.main()
