"""Regressions for the ``<partition>_info.txt`` manifest consumed by repacking.

``load_image_json`` opened the manifest with ``"a+"`` (creating it when absent,
requiring write access) and parsed it unguarded, so an empty or truncated file
aborted the repack with a bare JSONDecodeError.
"""
import json
import tempfile
import unittest
from pathlib import Path

from Scripts.Primary.WorkSpace import load_image_json

MANIFEST = {"a": 1000, "b": 4096, "c": 32768, "d": "system", "s": 8388608}


class LoadImageJsonTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.source = self.root / "system"
        self.source.mkdir()
        self.path = self.root / "system_info.txt"

    def _write(self, payload):
        if isinstance(payload, str):
            self.path.write_text(payload, encoding="utf-8")
        else:
            self.path.write_text(json.dumps(payload), encoding="utf-8")

    def test_valid_manifest_is_returned_in_order(self):
        self._write(MANIFEST)
        fsize, dsize, inodes, block_size, blocks, per_group, mount_point = (
            load_image_json(str(self.path), str(self.source))
        )
        self.assertEqual((fsize, inodes, block_size, blocks, per_group), (
            8388608, 1000, 4096, 2048, 32768))
        self.assertEqual(mount_point, "/system")
        self.assertEqual(dsize, 8388608)

    def test_root_mount_point_is_not_prefixed(self):
        self._write({**MANIFEST, "d": "/"})
        self.assertEqual(load_image_json(str(self.path), str(self.source))[6], "/")

    def test_empty_manifest_reports_a_clear_error(self):
        self._write("")
        with self.assertRaises(RuntimeError) as caught:
            load_image_json(str(self.path), str(self.source))
        self.assertIn("无法解析", str(caught.exception))

    def test_truncated_manifest_reports_a_clear_error(self):
        self._write('{"a": 1, "b":')
        with self.assertRaises(RuntimeError):
            load_image_json(str(self.path), str(self.source))

    def test_missing_field_is_reported(self):
        payload = {key: value for key, value in MANIFEST.items() if key != "c"}
        self._write(payload)
        with self.assertRaises(RuntimeError) as caught:
            load_image_json(str(self.path), str(self.source))
        self.assertIn("c", str(caught.exception))

    def test_non_object_manifest_is_reported(self):
        self._write([1, 2, 3])
        with self.assertRaises(RuntimeError):
            load_image_json(str(self.path), str(self.source))

    def test_zero_block_size_is_reported(self):
        self._write({**MANIFEST, "b": 0})
        with self.assertRaises(RuntimeError):
            load_image_json(str(self.path), str(self.source))

    def test_missing_file_is_reported(self):
        with self.assertRaises(RuntimeError):
            load_image_json(str(self.root / "absent_info.txt"), str(self.source))


if __name__ == "__main__":
    unittest.main()
