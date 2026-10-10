"""OTA partition classification and heap-entry truthiness regressions.

Staged images used to be classified with ``Path.stem``/``rsplit('.', 1)[0]``,
so ``system.sparse.img`` became ``system.sparse`` and ``system_a.img`` kept its
slot; neither name is listed by ``avbroot ota list``, so the partition the user
asked to replace was handed to avbroot as a brand new one.  The CLI and the
GUI/MCP paths now share ``classify_ota_parts``.

``ReMake/dat_br.py``'s ``HeapItem.__bool__`` was inverted, so the "best next
vertex" loop could never break on a live entry.
"""
import unittest
from pathlib import Path

from Scripts.Primary.WorkSpace import classify_ota_parts
from Scripts.ReMake.dat_br import HeapItem
from Scripts.application import _ota_image_args

_classify_parts = classify_ota_parts


class ClassifyPartsTests(unittest.TestCase):
    def test_sparse_suffix_is_stripped_before_matching(self):
        replace, new = _classify_parts(["system.sparse.img"], {"system"})
        self.assertEqual(replace, [("system", "system.sparse.img")])
        self.assertEqual(new, [])

    def test_slot_suffix_is_matched_against_the_ota_name(self):
        replace, new = _classify_parts(["system_a.img"], {"system"})
        self.assertEqual(replace, [("system", "system_a.img")])
        self.assertEqual(new, [])

    def test_combined_sparse_and_slot_suffixes(self):
        replace, new = _classify_parts(["system_a.sparse.img"], {"system"})
        self.assertEqual(replace, [("system", "system_a.sparse.img")])
        self.assertEqual(new, [])

    def test_exact_name_wins_over_the_slot_stripped_one(self):
        replace, _ = _classify_parts(["system_a.img"], {"system_a"})
        self.assertEqual(replace, [("system_a", "system_a.img")])

    def test_unknown_partition_is_still_added(self):
        replace, new = _classify_parts(["vendor.img"], {"system"})
        self.assertEqual(replace, [])
        self.assertEqual(new, [("vendor", "vendor.img", None)])

    def test_unparsable_name_is_treated_as_a_new_partition(self):
        replace, new = _classify_parts(["my backup.img"], {"system"})
        self.assertEqual(replace, [])
        self.assertEqual(new, [("my backup", "my backup.img", None)])

    def test_mixed_input_is_split_correctly(self):
        replace, new = _classify_parts(
            ["system.sparse.img", "product.img", "system_a.img"], {"system", "product"}
        )
        self.assertEqual(
            replace, [("system", "system.sparse.img"), ("product", "product.img"),
                      ("system", "system_a.img")]
        )
        self.assertEqual(new, [])


class FakeVertex:
    def __init__(self, score):
        self.score = score


class OtaImageArgsTests(unittest.TestCase):
    """The GUI/MCP path must classify staged images exactly like the CLI."""

    @staticmethod
    def _images(*names):
        return [Path("/staged") / name for name in names]

    def test_sparse_image_replaces_the_ota_partition(self):
        args, added, _ = _ota_image_args(self._images("system.sparse.img"), {"system"}, {})
        self.assertEqual(
            args, ["--replace", "system", str(Path("/staged/system.sparse.img"))])
        self.assertEqual(added, [])

    def test_slot_suffixed_image_replaces_the_ota_partition(self):
        args, added, _ = _ota_image_args(self._images("vendor_a.img"), {"vendor"}, {})
        self.assertEqual(args[:2], ["--replace", "vendor"])
        self.assertEqual(added, [])

    def test_unknown_image_is_added_with_its_size(self):
        args, added, _ = _ota_image_args(
            self._images("myproduct.img"), {"system"}, {"myproduct": 4096})
        self.assertEqual(args, [
            "--add-partition", "myproduct", str(Path("/staged/myproduct.img")), "4096"])
        self.assertEqual(added, ["myproduct"])

    def test_size_may_be_keyed_by_the_file_stem(self):
        args, _, _ = _ota_image_args(
            self._images("myproduct.sparse.img"), {"system"}, {"myproduct.sparse": 8192})
        self.assertEqual(args[-1], "8192")

    def test_staged_names_accept_the_file_stem_for_super_mode(self):
        _, _, staged = _ota_image_args(self._images("vendor_a.img"), {"system"}, {})
        self.assertIn("vendor_a", staged)

    def test_staged_names_include_new_partitions(self):
        _, _, staged = _ota_image_args(self._images("myproduct.img"), {"system"}, {})
        self.assertIn("myproduct", staged)


class HeapItemTruthinessTests(unittest.TestCase):
    def test_live_entry_is_truthy(self):
        entry = HeapItem(FakeVertex(3))
        self.assertTrue(bool(entry))

    def test_cleared_entry_is_falsy(self):
        entry = HeapItem(FakeVertex(3))
        entry.clear()
        self.assertFalse(bool(entry))


if __name__ == "__main__":
    unittest.main()
