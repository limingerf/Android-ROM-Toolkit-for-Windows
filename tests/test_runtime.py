"""Regressions for backend selection and mixed-encoding tool pipes."""
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from Scripts.Platform.runtime import (
    Toolchain, ToolchainError, _ToolOutputDecoder, _decode_tool_output,
    _print_tool_output, detect_toolchain,
)


class ToolOutputTests(unittest.TestCase):
    def test_wsl_warning_followed_by_utf8_with_every_chunk_boundary(self):
        warning = "wsl: 检测到 localhost 代理配置。\r\n"
        banner = "e2fsck 1.47.2 (1-Jan-2025)\n"
        payload = warning.encode("utf-16le") + banner.encode("utf-8")
        expected = [warning, banner]
        for boundary in range(len(payload) + 1):
            decoder = _ToolOutputDecoder()
            lines = decoder.feed(payload[:boundary])
            lines += decoder.feed(payload[boundary:], final=True)
            self.assertEqual(lines, expected, f"split at {boundary}")

    def test_utf16be_and_bom_with_single_byte_chunks(self):
        for encoding, bom in (("utf-16le", b"\xff\xfe"), ("utf-16be", b"\xfe\xff")):
            warning = "wsl: 中文提示\n"
            data = bom + warning.encode(encoding) + b"Pass 1: Checking inodes\n"
            decoder = _ToolOutputDecoder()
            lines = []
            for byte in data:
                lines.extend(decoder.feed(bytes([byte])))
            lines.extend(decoder.feed(b"", final=True))
            self.assertEqual(lines, [warning, "Pass 1: Checking inodes\n"])

    def test_stray_nul_does_not_turn_ascii_into_utf16(self):
        self.assertEqual(_decode_tool_output(b"\x00e2fsck 1.47.2\n"), "e2fsck 1.47.2\n")

    def test_short_utf8_before_utf16_and_embedded_lf_byte(self):
        warning = "wsl: \u010a提示\r\n"
        data = b"OK\n" + warning.encode("utf-16le") + b"e2fsck\n"
        for boundary in range(len(data) + 1):
            decoder = _ToolOutputDecoder()
            lines = decoder.feed(data[:boundary]) + decoder.feed(data[boundary:], final=True)
            self.assertEqual(lines, ["OK\n", warning, "e2fsck\n"])

    def test_utf8_multibyte_and_unterminated_final_line(self):
        decoder = _ToolOutputDecoder()
        lines = []
        for byte in "正在回包\n工具输出尾行".encode("utf-8"):
            lines += decoder.feed(bytes([byte]))
        lines += decoder.feed(b"", final=True)
        self.assertEqual(lines, ["正在回包\n", "工具输出尾行"])

    def test_console_backspaces_are_applied(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            _print_tool_output("Writing: 0/90\b\b\b\b     \b\b\b\bdone\n")
        self.assertNotIn("\b", out.getvalue())
        self.assertIn("done", out.getvalue())

    def test_both_pipes_drain_and_nonzero_exit_is_preserved(self):
        script = (
            "import sys; "
            "sys.stdout.buffer.write(('输出\\n' * 20000).encode()); sys.stdout.flush(); "
            "sys.stderr.buffer.write(('诊断\\n' * 20000).encode()); sys.stderr.flush(); "
            "sys.exit(7)"
        )
        toolchain = Toolchain("native", Path.cwd(), Path.cwd())
        self.assertEqual(toolchain.run([sys.executable, "-c", script], bundled=False, output=False), 7)

    def test_capture_decodes_mixed_encoding_and_text_input(self):
        toolchain = Toolchain("native", Path.cwd(), Path.cwd())
        script = (
            "import sys; data=sys.stdin.buffer.read(); "
            "sys.stdout.buffer.write('wsl: 提示\\n'.encode('utf-16le') + data)"
        )
        result = toolchain.capture([sys.executable, "-c", script], capture_output=True,
                                   text=True, universal_newlines=True, input="中文尾行\n")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "wsl: 提示\n中文尾行\n")


class BackendTests(unittest.TestCase):
    def test_native_never_falls_back_to_bundled_linux_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            native = root / "native"
            linux = root / "linux"
            native.mkdir()
            linux.mkdir()
            (linux / "e2fsck").touch()
            toolchain = Toolchain("native", root, native, (native,), "wsl.exe", linux)
            with patch("Scripts.Platform.runtime.shutil.which", return_value=None):
                self.assertIsNone(toolchain.resolve("e2fsck", required=False))
                with self.assertRaises(ToolchainError):
                    toolchain.command(["e2fsck", "-fn", "system.img"])
            self.assertEqual(toolchain.label, "Windows 原生")

    @unittest.skipUnless(os.name == "nt", "Windows-specific native bridge")
    def test_extended_ota_flags_use_embedded_bridge_and_translate_super_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            native = root / "art-res" / "bin-win-amd64"
            linux = root / "art-res" / "bin-amd64"
            native.mkdir(parents=True)
            linux.mkdir(parents=True)
            (native / "avbroot.exe").write_bytes(b"windows")
            (linux / "avbroot").write_bytes(b"linux")
            toolchain = Toolchain("native", root, native, (native,), "wsl.exe", linux)
            with patch("Scripts.Platform.runtime.shutil.which", return_value="wsl.exe"):
                command = toolchain.command([
                    "avbroot", "ota", "patch", "--add-partition", "vendor_dlkm",
                    r"E:\art\vendor_dlkm.img", "--super-mode", "vendor_dlkm",
                    "--disable-avb",
                ])
            self.assertEqual(command[0], "wsl.exe")
            self.assertIn("--dynamic-partition", command)
            self.assertNotIn("--super-mode", command)
            self.assertIn("/mnt/e/art/vendor_dlkm.img", command)

    @unittest.skipUnless(os.name == "nt", "Windows-specific detection")
    def test_native_detection_and_explicit_wsl_selection(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"ART_BACKEND": "native"}):
            root = Path(directory)
            native = root / "art-res" / "bin-win-amd64"
            checker = native / "e2fsprogs"
            checker.mkdir(parents=True)
            (checker / "e2fsck.exe").touch()
            linux = root / "art-res" / "bin-amd64"
            linux.mkdir()
            (linux / "e2fsck").touch()
            toolchain = detect_toolchain(root)
            self.assertEqual(toolchain.mode, "native")
            self.assertIsNone(toolchain.wsl_executable)
            self.assertEqual(toolchain.command(["e2fsck", "-fn", "system.img"])[0], str(checker / "e2fsck.exe"))
            os.environ["ART_BACKEND"] = "wsl"
            with patch("Scripts.Platform.runtime.shutil.which", return_value="wsl.exe"):
                command = detect_toolchain(root).command(["e2fsck", "-fn", "E:\\art\\system.img"])
            self.assertEqual(command[0], "wsl.exe")
            self.assertIn("/mnt/e/art/system.img", command)

    def test_saved_choice_overrides_inherited_backend(self):
        from Scripts.application import ArtController
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"ART_BACKEND": "wsl"}):
            controller = ArtController(directory)
            controller.configure_tools("", "native")
            self.assertEqual(os.environ["ART_BACKEND"], "native")
            self.assertNotEqual(controller.toolchain.mode, "wsl")
            request = controller.job_request("convert")
            self.assertEqual(request["backend"], controller.toolchain.mode)


if __name__ == "__main__":
    unittest.main()
