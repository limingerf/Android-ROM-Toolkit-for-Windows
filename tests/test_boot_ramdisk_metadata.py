"""Metadata round-trip tests for boot ramdisk repacking.

NTFS cannot carry cpio permission bits or symlinks, so the extractor records
them in ``ramdisk.meta.json`` and the repacker writes them back.  Without this
an init_boot ramdisk repacked on Windows lost ``/init``'s exec bit and turned
``system/bin/snapuserd`` into an empty regular file.
"""
import json
import stat
import tempfile
import unittest
from pathlib import Path

from Scripts.Extract import boot as extract_boot
from Scripts.ReMake import boot as remake_boot


def _pad(buffer):
    while len(buffer) % 4:
        buffer.append(0)


def build_newc(entries):
    """Serialize ``(name, mode, body, rdev)`` tuples as a newc archive."""
    archive = bytearray()
    for index, (name, mode, body, rdev) in enumerate(entries, 1):
        name_bytes = name.encode("utf-8") + b"\0"
        fields = [index, mode, 0, 0, 1, 0, len(body), 0, 0,
                  rdev[0], rdev[1], len(name_bytes), 0]
        archive += b"070701" + b"".join(f"{value:08x}".encode("ascii")
                                        for value in fields)
        archive += name_bytes
        _pad(archive)
        archive += body
        _pad(archive)
    name_bytes = b"TRAILER!!!\0"
    fields = [0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, len(name_bytes), 0]
    archive += b"070701" + b"".join(f"{value:08x}".encode("ascii")
                                    for value in fields)
    archive += name_bytes
    _pad(archive)
    return bytes(archive)


def parse_newc(archive):
    """Parse a newc archive, asserting that the trailer is the last record."""
    entries = {}
    offset = 0
    while True:
        header = archive[offset:offset + 110]
        if len(header) < 110 or header[:6] != b"070701":
            raise AssertionError("bad cpio header")
        fields = [int(header[6 + index * 8:14 + index * 8], 16)
                  for index in range(13)]
        namesize, filesize = fields[11], fields[6]
        name = archive[offset + 110:offset + 110 + namesize].rstrip(b"\0")
        name_end = offset + 110 + namesize
        name_end += (-name_end) % 4
        body = archive[name_end:name_end + filesize]
        body_end = name_end + filesize
        body_end += (-body_end) % 4
        text = name.decode("utf-8")
        if text == "TRAILER!!!":
            if body_end != len(archive):
                raise AssertionError("records follow the trailer")
            return entries
        entries[text] = {
            "mode": fields[1],
            "uid": fields[2],
            "gid": fields[3],
            "rdev": (fields[9], fields[10]),
            "body": body,
        }
        offset = body_end


class CpioMetadataReadTests(unittest.TestCase):
    def test_symlink_and_device_metadata_are_captured(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "ramdisk.cpio"
            archive.write_bytes(build_newc([
                ("dev", stat.S_IFDIR | 0o755, b"", (0, 0)),
                ("dev/null", stat.S_IFCHR | 0o644, b"", (1, 3)),
                ("init", stat.S_IFREG | 0o750, b"ELF", (0, 0)),
                ("system/bin/snapuserd", stat.S_IFLNK | 0o777,
                 b"snapuserd_ramdisk", (0, 0)),
            ]))
            entries = extract_boot._read_cpio_entries(str(archive))
            self.assertEqual(entries["init"]["mode"], stat.S_IFREG | 0o750)
            self.assertEqual(entries["dev/null"]["rdev"], [1, 3])
            self.assertEqual(entries["system/bin/snapuserd"]["link"],
                             "snapuserd_ramdisk")

    def test_non_cpio_payload_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "ramdisk.cpio"
            archive.write_bytes(b"\xeb\x58\x90mkfs.fat")
            self.assertIsNone(extract_boot._read_cpio_entries(str(archive)))


class MetadataSidecarTests(unittest.TestCase):
    def test_sidecar_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "ramdisk.cpio").write_bytes(build_newc([
                ("init", stat.S_IFREG | 0o750, b"ELF", (0, 0)),
            ]))
            self.assertTrue(extract_boot._save_ramdisk_metadata(
                str(work / "ramdisk.cpio"),
                str(work / extract_boot.RAMDISK_METADATA_NAME)))
            payload = json.loads(
                (work / extract_boot.RAMDISK_METADATA_NAME).read_text("utf-8"))
            self.assertEqual(payload["entries"]["init"]["mode"],
                             stat.S_IFREG | 0o750)
            loaded = remake_boot._load_ramdisk_metadata(str(work))
            self.assertIn("init", loaded)

    def test_missing_sidecar_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(remake_boot._load_ramdisk_metadata(tmp))


class RestoreMetadataTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "ramdisk.cpio"

    def _manifest(self):
        return {
            "dev": {"mode": stat.S_IFDIR | 0o755, "uid": 0, "gid": 0,
                    "rdev": [0, 0], "link": None},
            "dev/null": {"mode": stat.S_IFCHR | 0o644, "uid": 0, "gid": 0,
                         "rdev": [1, 3], "link": None},
            "init": {"mode": stat.S_IFREG | 0o750, "uid": 0, "gid": 0,
                     "rdev": [0, 0], "link": None},
            "system/bin/snapuserd": {"mode": stat.S_IFLNK | 0o777, "uid": 0,
                                     "gid": 0, "rdev": [0, 0],
                                     "link": "snapuserd_ramdisk"},
            "system/etc/old.rc": {"mode": stat.S_IFREG | 0o644, "uid": 0,
                                  "gid": 0, "rdev": [0, 0], "link": None},
        }

    def test_windows_permissions_and_links_are_restored(self):
        # What busybox emits on Windows: no device nodes, everything 0664,
        # the symlink flattened to an empty file, and a user-added file.
        self.path.write_bytes(build_newc([
            ("dev", stat.S_IFDIR | 0o775, b"", (0, 0)),
            ("init", stat.S_IFREG | 0o664, b"ELF", (0, 0)),
            ("system/bin/snapuserd", stat.S_IFREG | 0o664, b"", (0, 0)),
            ("system/etc/new.rc", stat.S_IFREG | 0o664, b"new", (0, 0)),
        ]))
        self.assertTrue(remake_boot._restore_ramdisk_metadata(
            str(self.path), self._manifest()))
        entries = parse_newc(self.path.read_bytes())
        self.assertEqual(entries["init"]["mode"], stat.S_IFREG | 0o750)
        self.assertEqual(entries["init"]["body"], b"ELF")
        self.assertEqual(entries["dev"]["mode"], stat.S_IFDIR | 0o755)
        self.assertEqual(entries["dev/null"]["mode"], stat.S_IFCHR | 0o644)
        self.assertEqual(entries["dev/null"]["rdev"], (1, 3))
        self.assertEqual(entries["system/bin/snapuserd"]["mode"],
                         stat.S_IFLNK | 0o777)
        self.assertEqual(entries["system/bin/snapuserd"]["body"],
                         b"snapuserd_ramdisk")
        # A file the user added keeps its packed mode; a deleted file stays
        # deleted instead of being resurrected from the sidecar.
        self.assertEqual(entries["system/etc/new.rc"]["mode"],
                         stat.S_IFREG | 0o664)
        self.assertNotIn("system/etc/old.rc", entries)

    def test_special_member_without_parent_is_not_resurrected(self):
        self.path.write_bytes(build_newc([
            ("init", stat.S_IFREG | 0o664, b"ELF", (0, 0)),
        ]))
        self.assertTrue(remake_boot._restore_ramdisk_metadata(
            str(self.path), {"dev/null": {"mode": stat.S_IFCHR | 0o644,
                                          "uid": 0, "gid": 0,
                                          "rdev": [1, 3], "link": None}}))
        self.assertNotIn("dev/null", parse_newc(self.path.read_bytes()))

    def test_bad_archive_leaves_original_untouched(self):
        original = b"\xeb\x58\x90mkfs.fat"
        self.path.write_bytes(original)
        self.assertFalse(remake_boot._restore_ramdisk_metadata(
            str(self.path), self._manifest()))
        self.assertEqual(self.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
