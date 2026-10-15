"""Regressions for repack failure reporting and Cygwin-style symlinks.

A repack used to fail with a bare "回包失败，未生成产物" whenever the build
tool exited non-zero, which hid the tool's own diagnosis (a missing fs_config
entry, a full disk, a file that cannot be read).  The failure must now carry the
exit code, the command and the tail of the tool output, so one log line is
enough to act on.

The second half covers the Cygwin-style symlinks the bundled extractor writes:
both bundled `mkfs.erofs` and `e2fsdroid` are Cygwin builds and rebuild a real
symlink from them, so they must be reported as complete rather than as links
that could not be restored.
"""
import tempfile
import unittest
from pathlib import Path

from Scripts.Platform import runtime
from Scripts.Primary.FileConfigPatcher import count_cygwin_symlinks, restore_symlinks
from Scripts.Primary.Utils import V
from Scripts.ReMake import erofs as remake_erofs
from Scripts.ReMake import ext4 as remake_ext4

MIB = 1024 * 1024
IMAGE_SIZE = 8 * MIB
CYGWIN_SYMLINK = b"!<symlink>" + "/system/bin".encode("utf-16-le") + b"\x00\x00"


class _GlobalValueTestCase(unittest.TestCase):
    """Share the V-global save/restore used by the repack helpers."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.input_dir = root / "INPUT"
        self.out_dir = root / "OUT"
        self.input_dir.mkdir()
        self.out_dir.mkdir()
        self._saved = (V.input, V.out, getattr(V, "workspace", None), dict(V.SETUP_MANIFEST))
        self.addCleanup(self._restore_globals)
        V.input = str(self.input_dir)
        V.out = str(self.out_dir)
        V.workspace = str(self.out_dir)
        V.SETUP_MANIFEST = {"SUPER_SIZE": "9126805504", "REPACK_SPARSE_IMG": "0",
                            "RESIZE_EROFSIMG": "1", "REPACK_EROFS_LEVEL": "9"}

    def _restore_globals(self):
        V.input, V.out, workspace, manifest = self._saved
        V.workspace = workspace
        V.SETUP_MANIFEST = manifest


class ToolFailureMessageTests(unittest.TestCase):
    def setUp(self):
        self._saved = list(runtime.TOOL_OUTPUT_TAIL)
        runtime.TOOL_OUTPUT_TAIL.clear()
        self.addCleanup(self._restore_tail)

    def _restore_tail(self):
        runtime.TOOL_OUTPUT_TAIL.clear()
        runtime.TOOL_OUTPUT_TAIL.extend(self._saved)

    def test_message_carries_exit_code_command_and_tool_output(self):
        runtime._print_tool_output("failed to find system/app/New/New.apk in canned fs_config")
        message = runtime.tool_failure_message("mkfs.erofs", 1, ["mkfs.erofs", "-zlz4hc,9"])
        self.assertIn("mkfs.erofs", message)
        self.assertIn("退出码 1", message)
        self.assertIn("-zlz4hc,9", message)
        self.assertIn("canned fs_config", message)

    def test_tail_keeps_only_the_last_lines(self):
        for index in range(200):
            runtime._print_tool_output(f"line {index}")
        tail = runtime.recent_tool_output(5).splitlines()
        self.assertEqual(tail, [f"line {index}" for index in range(195, 200)])

    def test_silent_failure_is_stated_explicitly(self):
        message = runtime.tool_failure_message("e2fsdroid", 1)
        self.assertIn("未输出任何错误信息", message)
        self.assertNotIn("工具输出末尾", message)


class CygwinSymlinkTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "WORKSPACE"
        self.partition = self.workspace / "system"
        (self.partition / "system" / "bin").mkdir(parents=True)
        (self.partition / "bin").write_bytes(CYGWIN_SYMLINK)
        (self.partition / "system" / "bin" / "sh").write_bytes(b"\x7fELF payload")
        self.fsconfig = self.workspace / "system_fsconfig.txt"
        self.fsconfig.write_text("system 0 0 0755\n", encoding="utf-8", newline="\n")

    def test_only_cygwin_style_files_are_counted(self):
        self.assertEqual(count_cygwin_symlinks(str(self.partition)), 1)

    def test_restore_report_exposes_the_count(self):
        report = restore_symlinks(str(self.partition), str(self.fsconfig))
        self.assertEqual(report["declared"], 0)
        self.assertEqual(report["cygwin"], 1)
        # A Cygwin link is complete on its own: nothing must be reported as
        # missing, which is what used to trigger the "symlinks were lost" alarm.
        self.assertEqual(report["failed"], [])


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


class _RepackFailureMixin:
    module = None

    def test_failure_raises_the_tool_message_and_removes_the_partial_image(self):
        original_call = self.module.call

        def fake_call(command):
            runtime._print_tool_output("canned fs_config: failed to find system/app/New/New.apk")
            return 1

        self.module.call = fake_call
        self.addCleanup(setattr, self.module, "call", original_call)
        with self.assertRaises(RuntimeError) as raised:
            self.module._write_image(_image_state(self.out_dir), "fsconfig.txt",
                                     "contexts.txt", "source", 8)
        message = str(raised.exception)
        self.assertIn("canned fs_config", message)
        self.assertIn("退出码 1", message)
        self.assertFalse((self.out_dir / "system_new.img").exists())


class ErofsRepackFailureTests(_RepackFailureMixin, _GlobalValueTestCase):
    module = remake_erofs


class Ext4RepackFailureTests(_RepackFailureMixin, _GlobalValueTestCase):
    module = remake_ext4

    def test_failure_raises_the_tool_message_and_removes_the_partial_image(self):
        original_call = self.module.call

        def fake_call(command):
            if command[0] == "mke2fs":
                Path(command[-2]).write_bytes(b"\0" * IMAGE_SIZE)
                return 0
            runtime._print_tool_output(
                "failed to find odm/etc/selinux/odm_sepolicy.cil in canned fs_config")
            return 1

        self.module.call = fake_call
        self.addCleanup(setattr, self.module, "call", original_call)
        with self.assertRaises(RuntimeError) as raised:
            self.module._write_image(_image_state(self.out_dir), "fsconfig.txt",
                                     "contexts.txt", "source", 8)
        message = str(raised.exception)
        self.assertIn("canned fs_config", message)
        self.assertIn("e2fsdroid", message)
        self.assertFalse((self.out_dir / "system_new.img").exists())


if __name__ == "__main__":
    unittest.main()
