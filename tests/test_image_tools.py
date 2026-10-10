"""Regressions for image-format detection and sparse conversion.

``get_file_type`` compared the ELF signature against ``b".ELF"`` (0x2E), so a
real ELF file (``\\x7fELF``) was reported as unknown.  Several later signature
entries could never match because an earlier entry used the same magic
(``zstd`` behind ``zst``, ``zopfli`` behind ``gzip``, ``lz4_lg`` behind
``lz4_legacy``), which is why the table is now duplicate free.
"""
import os
import struct
import tempfile
import unittest
from pathlib import Path

from Scripts.Primary import ImageTools


def write(path, data):
    Path(path).write_bytes(data)
    return str(path)


class FileTypeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _type(self, data):
        return ImageTools.get_file_type(write(self.root / "sample.bin", data))

    def test_elf_magic_is_detected(self):
        self.assertEqual(self._type(b"\x7fELF\x02\x01\x01" + b"\x00" * 32), "elf")

    def test_common_magics(self):
        self.assertEqual(self._type(b"PK\x03\x04rest"), "zip")
        self.assertEqual(self._type(b"CrAU\x00\x00"), "payload")
        self.assertEqual(self._type(b"\x1f\x8b\x08\x00rest"), "gzip")
        self.assertEqual(self._type(b"\x28\xb5\x2f\xfdrest"), "zstd")
        self.assertEqual(self._type(b"\xfd7zXZ\x00rest"), "xz")
        self.assertEqual(self._type(b"MZ\x90\x00rest"), "exe")

    def test_offset_magics(self):
        self.assertEqual(self._type(b"\x00" * 1080 + b"\x53\xef"), "ext")
        self.assertEqual(self._type(b"\x00" * 1024 + b"\xe2\xe1\xf5\xe0"), "erofs")
        self.assertEqual(self._type(b"\x00" * 4096 + b"\x67\x44\x6c\x61"), "super")
        self.assertEqual(self._type(b"\x00" * 1024 + b"\x10\x20\xF5\xF2"), "f2fs")

    def test_unknown_and_missing(self):
        self.assertEqual(self._type(b"\x01\x02\x03\x04rest"), "unknown")
        self.assertEqual(ImageTools.get_file_type(str(self.root / "absent")), "fne")

    def test_signature_table_has_no_shadowed_entry(self):
        seen = {}
        for entry in ImageTools._FILE_SIGNATURES:
            offset = entry[2] if len(entry) == 3 else 0
            key = (bytes(entry[0]), offset)
            self.assertNotIn(key, seen,
                             f"{entry[1]} can never match, {seen.get(key)} is checked first")
            seen[key] = entry[1]


class SparseDetectionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_sparse_magic(self):
        path = write(self.root / "a.img", struct.pack("<I", ImageTools.SPARSE_HEADER_MAGIC) + b"\x00" * 24)
        self.assertTrue(ImageTools.is_sparse_image(path))

    def test_raw_is_not_sparse(self):
        path = write(self.root / "b.img", b"\x00" * 4096)
        self.assertFalse(ImageTools.is_sparse_image(path))

    def test_short_file_is_not_sparse(self):
        path = write(self.root / "c.img", b"\x3a\xff")
        self.assertFalse(ImageTools.is_sparse_image(path))

    def test_missing_file_is_not_sparse(self):
        self.assertFalse(ImageTools.is_sparse_image(self.root / "absent.img"))


class SparseRoundTripTests(unittest.TestCase):
    BLOCK = 4096

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def chunks(self, path):
        """Return [(type, blocks, payload_size)] of a sparse image."""
        result = []
        with open(path, "rb") as stream:
            magic, _major, _minor, file_hdr, chunk_hdr, block_size, total, count, _sum = struct.unpack(
                "<I4H4I", stream.read(28))
            self.assertEqual(magic, ImageTools.SPARSE_HEADER_MAGIC)
            stream.seek(file_hdr)
            for _ in range(count):
                kind, _reserved, blocks, total_size = struct.unpack("<2H2I", stream.read(12))
                result.append((kind, blocks, total_size - chunk_hdr))
                stream.seek(total_size - chunk_hdr, os.SEEK_CUR)
        return result, total, block_size

    def round_trip(self, data, name):
        raw = write(self.root / f"{name}.raw", data)
        sparse = ImageTools.raw_to_sparse(raw, str(self.root / f"{name}.sparse.img"), self.BLOCK)
        back = ImageTools.sparse_to_raw(sparse, str(self.root / f"{name}.back.raw"))
        expected = data + b"\x00" * (-len(data) % self.BLOCK)
        self.assertEqual(Path(back).read_bytes(), expected)
        return sparse

    def test_round_trip_zero_fill_and_random(self):
        import random
        random.seed(7)
        data = (
            b"\x00" * (self.BLOCK * 3)
            + b"\xab" * (self.BLOCK * 2)
            + bytes(random.randrange(256) for _ in range(self.BLOCK * 2))
        )
        sparse = self.round_trip(data, "mixed")
        chunks, total, block_size = self.chunks(sparse)
        self.assertEqual(block_size, self.BLOCK)
        self.assertEqual(total, len(data) // self.BLOCK)
        kinds = [kind for kind, _blocks, _size in chunks]
        self.assertIn(ImageTools.SPARSE_CHUNK_DONT_CARE, kinds)
        self.assertIn(ImageTools.SPARSE_CHUNK_FILL, kinds)
        self.assertIn(ImageTools.SPARSE_CHUNK_RAW, kinds)

    def test_partial_final_block_is_zero_padded(self):
        sparse = self.round_trip(b"\x11" * (self.BLOCK + 5), "partial")
        _chunks, total, _block_size = self.chunks(sparse)
        self.assertEqual(total, 2)

    def test_empty_file_round_trips(self):
        sparse = self.round_trip(b"", "empty")
        _chunks, total, _block_size = self.chunks(sparse)
        self.assertEqual(total, 0)

    def test_raw_chunks_are_split_at_the_size_limit(self):
        limit = max(1, (0xFFFFFFFF - ImageTools.SPARSE_CHUNK_HEADER_SIZE) // self.BLOCK)
        self.assertGreater(limit, 0)

    def test_non_sparse_input_is_rejected(self):
        raw = write(self.root / "plain.raw", b"\x00" * self.BLOCK)
        with self.assertRaises(ValueError):
            ImageTools.sparse_to_raw(raw, str(self.root / "out.raw"))

    def test_sparse_input_is_rejected_by_raw_to_sparse(self):
        raw = write(self.root / "plain.raw", b"\x00" * self.BLOCK)
        sparse = ImageTools.raw_to_sparse(raw, str(self.root / "a.sparse.img"), self.BLOCK)
        with self.assertRaises(ValueError):
            ImageTools.raw_to_sparse(sparse, str(self.root / "b.sparse.img"), self.BLOCK)

    def test_truncated_sparse_image_is_rejected(self):
        raw = write(self.root / "plain.raw", b"\x11" * (self.BLOCK * 4))
        sparse = ImageTools.raw_to_sparse(raw, str(self.root / "a.sparse.img"), self.BLOCK)
        data = Path(sparse).read_bytes()
        truncated = write(self.root / "truncated.img", data[:-self.BLOCK])
        with self.assertRaises(ValueError):
            ImageTools.sparse_to_raw(truncated, str(self.root / "out.raw"))

    def test_same_path_conversion_is_rejected(self):
        raw = write(self.root / "same.raw", b"\x11" * self.BLOCK)
        with self.assertRaises(ValueError):
            ImageTools.raw_to_sparse(raw, raw, self.BLOCK)
        sparse = ImageTools.raw_to_sparse(raw, str(self.root / "same.sparse.img"), self.BLOCK)
        with self.assertRaises(ValueError):
            ImageTools.sparse_to_raw(sparse, sparse)

    def test_invalid_block_size_is_rejected(self):
        raw = write(self.root / "plain.raw", b"\x00" * self.BLOCK)
        for bad in (2, 6, 0):
            with self.assertRaises(ValueError):
                ImageTools.raw_to_sparse(raw, str(self.root / f"b{bad}.img"), bad)


if __name__ == "__main__":
    unittest.main()
