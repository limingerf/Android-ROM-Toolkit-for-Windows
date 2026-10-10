"""zstd ramdisks must not depend on magiskboot.

The bundled magiskboot answers ``Unknown compression method: [zstd]`` and exits
non-zero, so a zstd ramdisk used to fail with "Decompress Ramdisk Fail...".
``Extract/boot.py`` now expands it with the ``zstandard`` module and records
``gzip`` for the repack step, which magiskboot can write back.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import zstandard

from Scripts.Extract import boot

PAYLOAD = b"070701" + b"ramdisk payload " * 64


class RamdiskDecompressionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def compressed(self, name="ramdisk.cpio"):
        path = self.root / name
        path.write_bytes(zstandard.ZstdCompressor().compress(PAYLOAD))
        return path

    def test_zstd_ramdisk_is_expanded_without_magiskboot(self):
        source = self.compressed()
        destination = self.root / "ramdisk.cpio.raw"
        with mock.patch.object(boot, "call") as runner:
            self.assertTrue(boot._decompress_ramdisk(source, destination, "zstd"))
        runner.assert_not_called()
        self.assertEqual(destination.read_bytes(), PAYLOAD)

    def test_other_formats_still_use_magiskboot(self):
        source = self.compressed("ramdisk.cpio.gz")
        destination = self.root / "out.cpio"
        with mock.patch.object(boot, "call", return_value=0) as runner:
            self.assertTrue(boot._decompress_ramdisk(source, destination, "gzip"))
        runner.assert_called_once()
        self.assertEqual(runner.call_args.args[0][0], "magiskboot")

    def test_magiskboot_failure_is_reported(self):
        source = self.compressed("ramdisk.cpio.lz4")
        with mock.patch.object(boot, "call", return_value=1):
            self.assertFalse(boot._decompress_ramdisk(source, self.root / "out", "lz4"))

    def test_truncated_zstd_payload_fails_cleanly(self):
        # copy_stream() reports a truncated frame as a normal end of stream, so
        # without the frame-end check a damaged ramdisk becomes an empty file.
        complete = zstandard.ZstdCompressor().compress(PAYLOAD)
        source = self.root / "broken.cpio"
        source.write_bytes(complete[:len(complete) // 2])
        destination = self.root / "out.cpio"
        self.assertFalse(boot._decompress_zstd(source, destination))
        self.assertFalse(destination.exists(), "失败时应删除半成品")

    def test_repack_compression_replaces_the_unsupported_format(self):
        self.assertEqual(boot._repack_compression("zstd"), "gzip")
        for supported in ("gzip", "lz4", "lz4_legacy", "xz", "lzma", "bzip2", "unknown"):
            self.assertEqual(boot._repack_compression(supported), supported)


if __name__ == "__main__":
    unittest.main()
