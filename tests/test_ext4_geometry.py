"""Regressions for EXT4 geometry handling on fixed-size partitions.

``vendor`` and ``odm`` live on fixed-size dynamic partitions.  A clean workspace
is published by copying the original image, which keeps the geometry, but an
edited workspace used to be refused outright because a plain ``mke2fs`` rebuild
produced a random UUID and mke2fs' own block/inode counts.  The rebuild now
reproduces the original superblock values, and these tests pin both halves of
that behaviour: reading the values back and feeding them to the tools.
"""
import contextlib
import io
import struct
import tempfile
import unittest
from pathlib import Path

from Scripts.Primary.Utils import V
from Scripts.ReMake import ext4 as remake_ext4

BLOCK_SIZE = 4096
BLOCKS = 19531
INODES = 4896
UUID = "e9c0a617-2b70-1a5b-908a-c38456c7e193"
LABEL = "odm"


def _superblock(block_size=BLOCK_SIZE, blocks=BLOCKS, inodes=INODES,
                uuid=UUID, label=LABEL, magic=0xEF53):
    """Build the 1024 bytes of an EXT4 superblock for the fields we read."""
    block = bytearray(1024)
    struct.pack_into("<I", block, 0, inodes)
    struct.pack_into("<I", block, 4, blocks & 0xFFFFFFFF)
    struct.pack_into("<I", block, 20, 0)
    struct.pack_into("<I", block, 24, (block_size // 1024).bit_length() - 1)
    struct.pack_into("<I", block, 32, 32768)
    struct.pack_into("<I", block, 40, inodes)
    struct.pack_into("<H", block, 56, magic)
    struct.pack_into("<I", block, 76, 1)
    struct.pack_into("<I", block, 84, 11)
    struct.pack_into("<H", block, 88, 256)
    struct.pack_into("<I", block, 92, 0x18)
    struct.pack_into("<I", block, 96, 0x42)
    struct.pack_into("<I", block, 100, 0x13)
    block[104:120] = bytes.fromhex(uuid.replace("-", ""))
    encoded = label.encode("utf-8")
    block[120:120 + len(encoded)] = encoded
    return bytes(block)


def _write_image(path, block=None, prefix=1024, trailing=512):
    """Write a file whose EXT4 superblock sits at ``prefix``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * prefix + (block if block is not None else _superblock())
                     + b"\0" * trailing)
    return path


class ReadExt4GeometryTests(unittest.TestCase):
    """`read_ext4_geometry` is the single source of the pinned values."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_reads_the_superblock_fields(self):
        image = _write_image(self.root / "odm.img")
        geometry = remake_ext4.read_ext4_geometry(str(image))
        self.assertEqual(geometry["block_size"], BLOCK_SIZE)
        self.assertEqual(geometry["blocks"], BLOCKS)
        self.assertEqual(geometry["inodes"], INODES)
        self.assertEqual(geometry["inode_size"], 256)
        self.assertEqual(geometry["uuid"], UUID)
        self.assertEqual(geometry["volume_name"], LABEL)
        self.assertEqual(geometry["size_bytes"], BLOCKS * BLOCK_SIZE)

    def test_missing_file_is_not_an_error(self):
        self.assertIsNone(remake_ext4.read_ext4_geometry(str(self.root / "gone.img")))

    def test_non_ext4_data_is_rejected(self):
        image = self.root / "random.img"
        image.write_bytes(b"\x00" * 4096)
        self.assertIsNone(remake_ext4.read_ext4_geometry(str(image)))

    def test_truncated_image_is_rejected(self):
        image = self.root / "short.img"
        image.write_bytes(b"\0" * 1032)
        self.assertIsNone(remake_ext4.read_ext4_geometry(str(image)))

    def test_a_sparse_container_has_no_readable_superblock(self):
        # Sparse images keep their chunks behind a 28-byte header, so the
        # caller must expand them first; the reader must not report garbage.
        image = self.root / "odm.sparse.img"
        image.write_bytes(struct.pack("<IHHHHIIII", 0xED26FF3A, 1, 0, 28, 12,
                                      BLOCK_SIZE, BLOCKS, 1, 0))
        self.assertIsNone(remake_ext4.read_ext4_geometry(str(image)))


class _RepackFixture(unittest.TestCase):
    """Share the V-globals save/restore and a small workspace."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.input_dir = self.root / "INPUT"
        self.out_dir = self.root / "OUT"
        self.source = self.root / "odm"
        self.input_dir.mkdir()
        self.out_dir.mkdir()
        (self.source / "etc").mkdir(parents=True)
        (self.source / "etc" / "build.prop").write_text("ro.odm=1\n", encoding="utf-8")
        self.fsconfig = self.root / "odm_fsconfig.txt"
        self.fsconfig.write_text("odm 0 0 0755\n", encoding="utf-8")
        self.contexts = self.root / "odm_contexts.txt"
        self.contexts.write_text("/odm(/.*)? u:object_r:odm_file:s0\n", encoding="utf-8")
        self._saved = (V.input, V.out, getattr(V, "workspace", None), dict(V.SETUP_MANIFEST))
        self.addCleanup(self._restore_globals)
        V.input = str(self.input_dir)
        V.out = str(self.out_dir)
        V.workspace = str(self.out_dir)
        V.SETUP_MANIFEST = {"SUPER_SIZE": "9126805504", "REPACK_SPARSE_IMG": "0",
                            "UTC": "1700000000"}

    def _restore_globals(self):
        V.input, V.out, workspace, manifest = self._saved
        V.workspace = workspace
        V.SETUP_MANIFEST = manifest

    def geometry(self):
        return remake_ext4.read_ext4_geometry(str(_write_image(self.input_dir / "odm.img")))


class PrepareGeometryTests(_RepackFixture):
    """`_prepare` must pin size and inode count to the original filesystem."""

    def _prepare(self, geometry):
        with contextlib.redirect_stdout(io.StringIO()):
            return remake_ext4._prepare(str(self.source), str(self.fsconfig),
                                        str(self.contexts), None, geometry=geometry)

    def test_geometry_pins_the_block_and_inode_counts(self):
        state = self._prepare(self.geometry())
        self.assertEqual(state["block_size"], BLOCK_SIZE)
        self.assertEqual(state["blocks"], BLOCKS)
        self.assertEqual(state["inodes"], INODES)
        self.assertEqual(state["size"], BLOCKS * BLOCK_SIZE)
        self.assertEqual(state["mount_point"], "/odm")

    def test_a_manifest_size_cannot_override_the_original_geometry(self):
        # REPACK_IMAGE_SIZE_BYTES is honoured for ordinary partitions; for a
        # fixed-size one it would make the image the wrong length.
        V.SETUP_MANIFEST["REPACK_IMAGE_SIZE_BYTES"] = str(64 * 1024 * 1024)
        state = self._prepare(self.geometry())
        self.assertEqual(state["blocks"], BLOCKS)
        self.assertEqual(state["size"], BLOCKS * BLOCK_SIZE)

    def test_without_geometry_the_manifest_size_still_wins(self):
        V.SETUP_MANIFEST["REPACK_IMAGE_SIZE_BYTES"] = str(64 * 1024 * 1024)
        state = self._prepare(None)
        self.assertEqual(state["size"], 64 * 1024 * 1024)
        self.assertIsNone(state["inodes"])


class WriteImageGeometryTests(_RepackFixture):
    """`_write_image` must hand the original geometry to mke2fs."""

    def _capture(self, geometry):
        state = {
            "label": "odm",
            "distance": str(self.out_dir / "odm.img"),
            "new_distance": str(self.out_dir / "odm_new.img"),
            "timestamp": 1700000000,
            "size": BLOCKS * BLOCK_SIZE,
            "read_mode": "rw",
            "mount_point": "/odm",
            "blocks": BLOCKS,
            "block_size": BLOCK_SIZE,
            "inode_size": 256,
            "inodes": INODES if geometry else None,
            "geometry": geometry,
        }
        commands = []

        def fake_call(command):
            commands.append(command)
            if command[0] == "mke2fs":
                Path(command[-2]).write_bytes(b"\0" * 4096)
            return 0

        original_call = remake_ext4.call
        remake_ext4.call = fake_call
        self.addCleanup(setattr, remake_ext4, "call", original_call)
        with contextlib.redirect_stdout(io.StringIO()):
            written = remake_ext4._write_image(state, str(self.fsconfig), str(self.contexts),
                                               str(self.source), 8)
        self.assertTrue(written)
        return commands

    def test_geometry_is_forwarded_to_mke2fs(self):
        commands = self._capture(self.geometry())
        mke2fs = next(command for command in commands if command[0] == "mke2fs")
        self.assertEqual(mke2fs[mke2fs.index("-b") + 1], str(BLOCK_SIZE))
        self.assertEqual(mke2fs[mke2fs.index("-N") + 1], str(INODES))
        self.assertEqual(mke2fs[mke2fs.index("-U") + 1], UUID)
        self.assertEqual(mke2fs[mke2fs.index("-L") + 1], LABEL)
        self.assertEqual(mke2fs[-1], str(BLOCKS))

    def test_without_geometry_the_build_stays_unchanged(self):
        commands = self._capture(None)
        mke2fs = next(command for command in commands if command[0] == "mke2fs")
        self.assertNotIn("-U", mke2fs)
        self.assertNotIn("-N", mke2fs)
        self.assertEqual(mke2fs[-1], str(BLOCKS))

    def test_e2fsdroid_receives_the_partition_mount_point(self):
        commands = self._capture(self.geometry())
        e2fsdroid = next(command for command in commands if command[0] == "e2fsdroid")
        self.assertEqual(e2fsdroid[e2fsdroid.index("-a") + 1], "/odm")
        self.assertEqual(e2fsdroid[-1], str(self.out_dir / "odm_new.img"))


if __name__ == "__main__":
    unittest.main()
