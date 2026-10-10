"""Regressions for shared filesystem helpers.

``get_dir_size`` walked the tree without an ``onerror`` callback, so a
directory that could not be read (or addressed, beyond MAX_PATH) was skipped
silently and the reported size came out too small.

``rmdire`` checked ``os.path.exists`` on the raw path, which is False beyond
MAX_PATH, so a deep tree was never removed.
"""
import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from Scripts.Primary import Utils


class GetDirSizeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_size_sums_regular_files(self):
        (self.root / "a.bin").write_bytes(b"a" * 1000)
        nested = self.root / "sub"
        nested.mkdir()
        (nested / "b.bin").write_bytes(b"b" * 500)
        self.assertEqual(Utils.get_dir_size(str(self.root), max_=1.0), 1500)

    def test_margin_is_applied(self):
        (self.root / "a.bin").write_bytes(b"a" * 1000)
        self.assertEqual(Utils.get_dir_size(str(self.root), max_=1.06), 1060)

    def test_walk_failure_is_reported_instead_of_ignored(self):
        def failing_walk(root, onerror=None, **kwargs):
            onerror(OSError(3, "系统找不到指定的路径", "C:\\deep\\path"))
            return iter(())

        buffer = io.StringIO()
        with mock.patch.object(Utils.os, "walk", failing_walk):
            with contextlib.redirect_stdout(buffer):
                size = Utils.get_dir_size("x", max_=1.0)
        self.assertEqual(size, 0)
        self.assertIn("无法统计目录大小", buffer.getvalue())
        self.assertIn("C:\\deep\\path", buffer.getvalue())


class RmdireTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_regular_tree_is_removed(self):
        target = self.root / "tree"
        (target / "sub").mkdir(parents=True)
        (target / "sub" / "a.bin").write_bytes(b"x")
        with contextlib.redirect_stdout(io.StringIO()):
            Utils.rmdire(str(target))
        self.assertFalse(target.exists())

    def test_missing_path_is_a_no_op(self):
        with contextlib.redirect_stdout(io.StringIO()):
            Utils.rmdire(str(self.root / "absent"))

    def test_deep_tree_beyond_max_path_is_removed(self):
        # TemporaryDirectory.cleanup() cannot remove ancestors that are beyond
        # MAX_PATH themselves, so this test owns its root and clears it through
        # the same helper.
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, Utils.extended_path(root), True)
        deep = os.path.join(root, *(["d" * 60] * 6))
        self.assertGreater(len(deep), Utils.LONG_PATH_LIMIT)
        target = Path(Utils.extended_path(deep))
        target.mkdir(parents=True)
        (target / "marker.bin").write_bytes(b"x")
        with contextlib.redirect_stdout(io.StringIO()):
            Utils.rmdire(deep)
        self.assertFalse(os.path.exists(Utils.extended_path(deep)))
        self.assertFalse((target / "marker.bin").exists())


if __name__ == "__main__":
    unittest.main()
