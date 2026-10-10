"""Regressions for repack sizing.

The dynamic-partition operation list must describe the image that will be
flashed, not the byte count of the file on disk: an ``img2simg`` output is a
sparse file whose size is unrelated to the partition it stands for.  The EXT4
and EROFS repackers share this logic, so both are covered.
"""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from Scripts.Primary.Utils import V
from Scripts.ReMake import erofs as remake_erofs
from Scripts.ReMake import ext4 as remake_ext4

MIB = 1024 * 1024
IMAGE_SIZE = 8 * MIB


class GlobalValueTestCase(unittest.TestCase):
    """Share the V-global save/restore used by the repack helpers."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.input_dir = root / "INPUT"
        self.out_dir = root / "OUT"
        self.input_dir.mkdir()
        self.out_dir.mkdir()
        self._saved = (
            V.input, V.out, getattr(V, "workspace", None), dict(V.SETUP_MANIFEST)
        )
        self.addCleanup(self._restore_globals)
        V.input = str(self.input_dir)
        V.out = str(self.out_dir)
        V.workspace = str(self.out_dir)
        V.SETUP_MANIFEST = {"SUPER_SIZE": "9126805504", "REPACK_SPARSE_IMG": "0"}

    def _restore_globals(self):
        V.input, V.out, workspace, manifest = self._saved
        V.workspace = workspace
        V.SETUP_MANIFEST = manifest


class _UpdateDynamicPartitionsMixin:
    """`resize <partition> <bytes>` must describe the image, not the file."""

    module = None

    def _write_op_list(self):
        (self.input_dir / "dynamic_partitions_op_list").write_text(
            "remove_all_groups\n"
            "add_group qti_dynamic_partitions_a 9126805504\n"
            "add system_a qti_dynamic_partitions_a\n"
            "resize system_a 2\n"
            "resize system_ext_a 2\n",
            encoding="utf-8",
            newline="\n",
        )

    def _resize_value(self, entry="system_a"):
        # The op list names the slotted partition (`system_a`), which is the
        # branch `_update_dynamic_partitions` rewrites for a `system` label.
        text = (self.out_dir / "dynamic_partitions_op_list").read_text(encoding="utf-8")
        prefix = f"resize {entry} "
        return next(line for line in text.splitlines() if line.startswith(prefix)).split()[-1]

    def test_explicit_image_size_wins_over_the_sparse_file_size(self):
        # A 44-byte file is what img2simg leaves behind for an 8 MiB image.
        self._write_op_list()
        sparse = self.out_dir / "system.img"
        sparse.write_bytes(b"\0" * 44)
        self.module._update_dynamic_partitions("system", str(sparse), IMAGE_SIZE)
        self.assertEqual(self._resize_value(), str(IMAGE_SIZE))

    def test_raw_image_still_falls_back_to_the_file_size(self):
        self._write_op_list()
        raw = self.out_dir / "system.img"
        raw.write_bytes(b"\0" * MIB)
        self.module._update_dynamic_partitions("system", str(raw))
        self.assertEqual(self._resize_value(), str(MIB))

    def test_unrelated_partition_lines_are_left_alone(self):
        self._write_op_list()
        raw = self.out_dir / "system.img"
        raw.write_bytes(b"\0" * MIB)
        self.module._update_dynamic_partitions("system", str(raw), 2 * MIB)
        self.assertEqual(self._resize_value("system_ext_a"), "2")

    def test_fabricated_list_has_no_two_byte_placeholders(self):
        # With no op list in INPUT the module builds one from scratch; it used
        # to seed `resize <part> 2` for every other partition, which would
        # shrink them to two bytes when the published list is used.
        raw = self.out_dir / "system.img"
        raw.write_bytes(b"\0" * MIB)
        self.module._update_dynamic_partitions("system", str(raw), IMAGE_SIZE)
        lines = (self.out_dir / "dynamic_partitions_op_list").read_text(
            encoding="utf-8").splitlines()
        self.assertIn(f"resize system_a {IMAGE_SIZE}", lines)
        self.assertEqual([line for line in lines if line.endswith(" 2")], [])

    def test_fabricated_list_uses_the_configured_group_name(self):
        V.SETUP_MANIFEST["GROUP_NAME"] = "google_dynamic_partitions"
        raw = self.out_dir / "system.img"
        raw.write_bytes(b"\0" * MIB)
        self.module._update_dynamic_partitions("system", str(raw), IMAGE_SIZE)
        text = (self.out_dir / "dynamic_partitions_op_list").read_text(encoding="utf-8")
        self.assertIn("add_group google_dynamic_partitions_a 9126805504", text)
        self.assertNotIn("qti_dynamic_partitions", text)

    def test_existing_list_gains_a_missing_resize_line(self):
        self._write_op_list()  # carries system_a and system_ext_a only
        raw = self.out_dir / "vendor.img"
        raw.write_bytes(b"\0" * MIB)
        self.module._update_dynamic_partitions("vendor", str(raw), 4 * MIB)
        self.assertEqual(self._resize_value("vendor_a"), str(4 * MIB))


class Ext4UpdateDynamicPartitionsTests(_UpdateDynamicPartitionsMixin, GlobalValueTestCase):
    module = remake_ext4


class ErofsUpdateDynamicPartitionsTests(_UpdateDynamicPartitionsMixin, GlobalValueTestCase):
    module = remake_erofs


def _image_state(out_dir):
    return {
        "label": "system",
        "distance": str(Path(out_dir) / "system.img"),
        "new_distance": str(Path(out_dir) / "system_new.img"),
        "timestamp": 0,
        "size": IMAGE_SIZE,
        "read_mode": "rw",
        "mount_point": "/system",
        "blocks": IMAGE_SIZE // 4096,
    }


class _WriteImageSizeMixin:
    """The raw image size must be captured before the sparse conversion."""

    module = None
    build_tool = None

    def _state(self):
        return _image_state(self.out_dir)

    def test_raw_size_is_captured_before_sparse_conversion(self):
        V.SETUP_MANIFEST = {
            "SUPER_SIZE": "9126805504",
            "REPACK_SPARSE_IMG": "1",
            "RESIZE_EROFSIMG": "1",
        }
        state = self._state()
        distance = Path(state["distance"])
        tool_calls = []
        original_call = self.module.call

        def fake_call(command):
            tool_calls.append(command[0])
            if command[0] in ("mke2fs", "mkfs.erofs"):
                # The real tool writes the whole (raw) image here.
                Path(command[-2]).write_bytes(b"\0" * IMAGE_SIZE)
                return 0
            if command[0] == "e2fsdroid":
                return 0
            if command[0] == "img2simg":
                Path(command[1]).unlink()
                Path(command[2]).write_bytes(b"\0" * 44)
                return 0
            return 1

        self.module.call = fake_call
        self.addCleanup(setattr, self.module, "call", original_call)
        with contextlib.redirect_stdout(io.StringIO()):
            written = self.module._write_image(state, "fsconfig.txt", "contexts.txt", "source", 8)
        self.assertTrue(written)
        self.assertIn(self.build_tool, tool_calls)
        self.assertIn("img2simg", tool_calls)
        self.assertEqual(distance.stat().st_size, 44, "test must exercise the sparse output")
        self.assertEqual(state["raw_size"], IMAGE_SIZE)


class Ext4WriteImageSizeTests(_WriteImageSizeMixin, GlobalValueTestCase):
    module = remake_ext4
    build_tool = "mke2fs"


class ErofsWriteImageSizeTests(_WriteImageSizeMixin, GlobalValueTestCase):
    module = remake_erofs
    build_tool = "mkfs.erofs"


class ErofsLegacyKernelTests(GlobalValueTestCase):
    """The pre-5.4 layout was removed upstream; the setting must say so."""

    def test_legacy_kernel_request_is_reported_as_ineffective(self):
        V.SETUP_MANIFEST = {
            "SUPER_SIZE": "9126805504",
            "REPACK_SPARSE_IMG": "0",
            "RESIZE_EROFSIMG": "1",
            "EROFS_OLD_KERNEL": "1",
        }
        commands = []
        original_call = remake_erofs.call

        def fake_call(command):
            commands.append(command)
            if command[0] == "mkfs.erofs":
                Path(command[-2]).write_bytes(b"\0" * IMAGE_SIZE)
            return 0

        remake_erofs.call = fake_call
        self.addCleanup(setattr, remake_erofs, "call", original_call)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            written = remake_erofs._write_image(
                _image_state(self.out_dir), "fsconfig.txt", "contexts.txt", "source", 8)
        self.assertTrue(written)
        mkfs = next(command for command in commands if command[0] == "mkfs.erofs")
        self.assertIn("legacy-compress", mkfs)
        self.assertIn("不会生效", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
