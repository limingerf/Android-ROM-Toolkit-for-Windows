"""End-to-end regression for DAT generation (``_image_to_dat``).

The transfer.list is the contract the OTA updater follows, so the test does not
just check that files appear: it re-parses the generated commands and asserts
that new/diff/zero/erase are disjoint and cover every block, and that the block
count of new/diff commands matches the byte size of ``.new.dat``.

A command argument is ``<value count>,start,end,start,end,...`` -- the same
syntax ``Scripts/Extract/dat_br.py::_rangeset`` consumes when rebuilding an
image, so both sides are checked against the same layout.
"""
import gc
import os
import tempfile
import unittest
import warnings
from pathlib import Path

from Scripts.Primary import ImageTools
from Scripts.ReMake.dat_br import _image_to_dat

BLOCK = 4096
VERSION = 4
# Commands that store data in .new.dat; the others only move or zero blocks.
DATA_COMMANDS = ("new", "diff")


def parse_range(spec):
    """Expand ``<count>,start,end,...`` into a set of blocks."""
    values = [int(item) for item in spec.split(",")]
    assert values, spec
    assert len(values) == values[0] + 1, f"值个数与范围不匹配: {spec}"
    blocks = set()
    for index in range(1, len(values), 2):
        start, end = values[index], values[index + 1]
        assert end >= start, spec
        blocks.update(range(start, end))
    return blocks


def parse_transfer_list(path):
    lines = [line for line in Path(path).read_text(encoding="utf-8").splitlines() if line]
    version = int(lines[0])
    total = int(lines[1])
    index = 2
    stash = max_stashed = None
    if version >= 2:
        stash, max_stashed = int(lines[2]), int(lines[3])
        index = 4
    commands = []
    for line in lines[index:]:
        name, spec = line.split()
        commands.append((name, parse_range(spec)))
    return version, total, stash, max_stashed, commands


class DatGenerationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.out = self.root / "out"

    def build(self, name, blocks):
        raw = self.root / f"{name}.raw"
        raw.write_bytes(b"".join(blocks))
        return ImageTools.raw_to_sparse(str(raw), str(self.root / f"{name}.img"), BLOCK)

    def generate(self, name, blocks):
        sparse = self.build(name, blocks)
        _image_to_dat(sparse, str(self.out), VERSION, name)
        transfer = self.out / f"{name}.transfer.list"
        new_dat = self.out / f"{name}.new.dat"
        self.assertTrue(transfer.is_file(), "缺少 transfer.list")
        self.assertTrue(new_dat.is_file(), "缺少 new.dat")
        return transfer, new_dat

    @staticmethod
    def stored_blocks(commands):
        return set().union(*(b for name, b in commands if name in DATA_COMMANDS))

    @staticmethod
    def stored_data(commands, blocks):
        """Concatenate the blocks in the order the transfer list consumes them.

        ``new.dat`` follows the command order produced by the block-diff graph,
        which is not necessarily ascending by block number.
        """
        data = bytearray()
        for name, spec in commands:
            if name in DATA_COMMANDS:
                for index in sorted(spec):
                    data += blocks[index]
        return bytes(data)

    def test_all_data_becomes_new_commands(self):
        blocks = [b"\x11" * BLOCK, b"\x22" * BLOCK, b"\x33" * BLOCK]
        transfer, new_dat = self.generate("full", blocks)
        version, total, _stash, _max, commands = parse_transfer_list(transfer)
        self.assertEqual(version, VERSION)
        self.assertEqual(total, len(blocks))

        self.assertEqual(self.stored_blocks(commands), {0, 1, 2})
        self.assertEqual(new_dat.read_bytes(), self.stored_data(commands, blocks))
        self.assertEqual(new_dat.stat().st_size, 3 * BLOCK)

    def test_zero_blocks_are_not_stored_as_data(self):
        blocks = [b"\x11" * BLOCK, b"\x00" * BLOCK, b"\x33" * BLOCK]
        transfer, new_dat = self.generate("holes", blocks)
        _version, total, _stash, _max, commands = parse_transfer_list(transfer)
        self.assertEqual(total, len(blocks))

        stored = self.stored_blocks(commands)
        self.assertEqual(stored, {0, 2})
        self.assertEqual(new_dat.read_bytes(), self.stored_data(commands, blocks))
        self.assertEqual(new_dat.stat().st_size, len(stored) * BLOCK)

    def test_commands_are_disjoint_and_cover_every_block(self):
        blocks = [b"\x11" * BLOCK, b"\x00" * BLOCK, b"\x33" * BLOCK, b"\x00" * BLOCK]
        transfer, new_dat = self.generate("cover", blocks)
        _version, total, _stash, _max, commands = parse_transfer_list(transfer)

        covered = set()
        for name, blocks_in_command in commands:
            self.assertFalse(covered & blocks_in_command,
                             f"{name} 与其他命令重叠: {sorted(blocks_in_command)}")
            covered |= blocks_in_command
        self.assertEqual(covered, set(range(total)))

        stored = self.stored_blocks(commands)
        self.assertEqual(new_dat.stat().st_size, len(stored) * BLOCK)
        self.assertEqual(new_dat.read_bytes(), self.stored_data(commands, blocks))

    def test_header_carries_version_total_and_stash_slots(self):
        transfer, _new_dat = self.generate("head", [b"\x11" * BLOCK])
        text = transfer.read_text(encoding="utf-8").splitlines()
        self.assertEqual(text[0], str(VERSION))
        self.assertEqual(text[1], "1")
        self.assertTrue(text[2].isdigit() and text[3].isdigit())

    def test_source_image_handle_is_closed(self):
        # Without an explicit close the SparseImage file object survives until
        # garbage collection, which is exactly what the caller's os.remove() of
        # the source image must not depend on (Windows keeps the file locked).
        sparse = self.build("closed", [b"\x11" * BLOCK])
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _image_to_dat(sparse, str(self.out), VERSION, "closed")
            gc.collect()
        leaked = [str(item.message) for item in caught
                  if issubclass(item.category, ResourceWarning)]
        self.assertEqual(leaked, [], f"镜像句柄未关闭: {leaked}")
        os.remove(sparse)


if __name__ == "__main__":
    unittest.main()
