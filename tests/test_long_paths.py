"""Regressions for Windows long-path handling.

With ``LongPathsEnabled=0`` a path longer than ``MAX_PATH`` cannot be created at
all, which a deep Android tree below a long project root easily reaches.  The
``\\\\?\\`` prefix lifts that limit for the paths A.R.T controls, and
``long_path_risk`` reports the problem before an extraction starts.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from Scripts.Primary import Utils


class ExtendedPathTests(unittest.TestCase):
    def test_short_absolute_path_is_still_prefixed_on_windows(self):
        result = Utils.extended_path(r"C:\tmp\a.img")
        if os.name == "nt":
            self.assertEqual(result, "\\\\?\\C:\\tmp\\a.img")
        else:
            self.assertEqual(result, r"C:\tmp\a.img")

    def test_prefix_is_not_added_twice(self):
        prefixed = "\\\\?\\C:\\tmp\\a.img"
        self.assertEqual(Utils.extended_path(prefixed), prefixed)

    def test_relative_path_is_made_absolute(self):
        result = Utils.extended_path("a/b")
        self.assertTrue(os.path.isabs(result.replace("\\\\?\\", "")))

    def test_deep_tree_can_be_created_through_the_helper(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, Utils.extended_path(tmp), True)
        segment = "d" * 60
        deep = os.path.join(tmp, *([segment] * 6))
        self.assertGreater(len(deep), Utils.LONG_PATH_LIMIT)
        target = Path(Utils.extended_path(deep))
        target.mkdir(parents=True)
        (target / "file.txt").write_text("ok", encoding="utf-8")
        self.assertTrue((target / "file.txt").is_file())

    def test_deep_tree_is_removed_through_the_helper(self):
        tmp = tempfile.mkdtemp()
        deep = os.path.join(tmp, *(["d" * 60] * 6))
        Path(Utils.extended_path(deep)).mkdir(parents=True)
        # rmtree needs the same prefix: without it the deep directories cannot
        # even be removed (WinError 145).
        shutil.rmtree(Utils.extended_path(tmp))
        self.assertFalse(os.path.exists(tmp))

    def test_plain_deep_tree_is_what_motivates_the_helper(self):
        if Utils.long_paths_enabled():
            self.skipTest("this machine allows long paths")
        with tempfile.TemporaryDirectory() as tmp:
            deep = os.path.join(tmp, *(["d" * 60] * 6))
            self.assertGreater(len(deep), Utils.LONG_PATH_LIMIT)
            with self.assertRaises(OSError):
                os.makedirs(deep)


class LongPathRiskTests(unittest.TestCase):
    def _risk(self, root, enabled, **kwargs):
        with mock.patch.object(Utils, "long_paths_enabled", return_value=enabled):
            return Utils.long_path_risk(root, **kwargs)

    def test_long_root_is_reported_when_the_policy_is_off(self):
        warning = self._risk("C:\\" + "p" * 200, enabled=False)
        self.assertIsNotNone(warning)
        self.assertIn("LongPathsEnabled", warning)

    def test_short_root_is_quiet(self):
        self.assertIsNone(self._risk("C:\\art\\proj", enabled=False))

    def test_nothing_is_reported_when_long_paths_are_enabled(self):
        self.assertIsNone(self._risk("C:\\" + "p" * 200, enabled=True))

    def test_margin_decides_the_boundary(self):
        root = "C:\\" + "p" * 100  # 103 characters
        self.assertIsNone(self._risk(root, enabled=False, margin=100))
        self.assertIsNotNone(self._risk(root, enabled=False, margin=200))


if __name__ == "__main__":
    unittest.main()
