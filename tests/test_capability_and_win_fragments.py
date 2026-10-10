"""Regressions for capability decoding and WIN fragment merging.

``Extract/ext4.py`` decoded ``security.capability`` with a fixed
``struct.unpack('<5I')``, so the 12-byte v1 and 24-byte v3 forms raised
struct.error and aborted the whole extraction.

``Extract/win.py`` concatenated ``<part>.win.NNN`` fragments in name order
without noticing a gap, publishing a truncated image as if it were complete.
"""
import struct
import unittest

from Scripts.Extract.ext4 import capability_field
from Scripts.Extract.win import _missing_fragments


class CapabilityFieldTests(unittest.TestCase):
    def test_v2_value_keeps_the_historical_encoding(self):
        value = struct.pack('<5I', 0x02000000, 0xFFFF, 0, 0, 0)
        self.assertEqual(capability_field(value), hex(0xFFFF))

    def test_wide_permitted_set_uses_the_two_word_form(self):
        value = struct.pack('<5I', 0x02000000, 0x00010000, 0, 0, 0)
        self.assertEqual(capability_field(value), hex(0x10000))

    def test_high_word_is_kept(self):
        value = struct.pack('<5I', 0x02000000, 0xFFFF, 0, 0x0002, 0)
        self.assertEqual(capability_field(value), hex(0x00020000FFFF))

    def test_v1_value_does_not_raise(self):
        value = struct.pack('<3I', 0x01000000, 0xFFFF, 0)
        self.assertEqual(capability_field(value), hex(0xFFFF))

    def test_v3_value_ignores_the_extra_rootid(self):
        v2 = struct.pack('<5I', 0x03000000, 0xFFFF, 0, 0, 0)
        v3 = v2 + struct.pack('<I', 0)
        self.assertEqual(len(v3), 24)
        self.assertEqual(capability_field(v3), capability_field(v2))

    def test_short_value_is_rejected_instead_of_raising(self):
        self.assertIsNone(capability_field(b'\x00' * 8))
        self.assertIsNone(capability_field(b''))


class MissingFragmentsTests(unittest.TestCase):
    def test_contiguous_series_is_complete(self):
        self.assertEqual(
            _missing_fragments(['system.win', 'system.win.001', 'system.win.002']), []
        )

    def test_single_unsuffixed_fragment_is_complete(self):
        self.assertEqual(_missing_fragments(['system.win']), [])

    def test_gap_is_reported(self):
        self.assertEqual(
            _missing_fragments(['system.win.001', 'system.win.003']), ['002']
        )

    def test_multiple_gaps_are_reported_in_order(self):
        self.assertEqual(
            _missing_fragments(
                ['system.win.001', 'system.win.002', 'system.win.005', 'system.win.006']
            ),
            ['003', '004'],
        )

    def test_unrelated_names_are_ignored(self):
        self.assertEqual(_missing_fragments(['system.win', 'system.bak']), [])


if __name__ == "__main__":
    unittest.main()
