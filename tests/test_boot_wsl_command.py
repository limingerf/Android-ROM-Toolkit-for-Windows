"""Regression for the WSL ramdisk packing command.

``Toolchain.run(shell=True)`` passes the whole shell string through
``windows_to_wsl()``, which rewrites one drive prefix per call.  The old code
built the string with ``os.sep`` replacement only, so the second Windows path
(the cpio output file) stayed as ``C:/...`` and could not be created inside
WSL; an unquoted path containing a space was also split by the shell.
"""
import re
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from Scripts.Platform.runtime import windows_to_wsl
from Scripts.ReMake import boot as remake_boot


class _FakeToolchain:
    mode = "wsl"

    def __init__(self, output=None):
        self.command = None
        self.output = output

    def run(self, command, **kwargs):
        self.command = command
        # _pack_ramdisk removes an existing archive first and then requires the
        # tool to have produced a non-empty one, so the fake writes it back.
        if self.output:
            Path(self.output).write_bytes(b"x" * 200)
        return 0


class WslRamdiskCommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _pack(self, busybox, output_cpio):
        toolchain = _FakeToolchain(output=output_cpio)
        with mock.patch.object(remake_boot, "_busybox_path", return_value=busybox):
            with mock.patch.object(remake_boot.V, "toolchain", toolchain):
                with mock.patch.object(remake_boot.os, "chdir", lambda path: None):
                    result = remake_boot._pack_ramdisk("C:/ramdisk", output_cpio)
        return result, toolchain.command

    def _tokens(self, command):
        return shlex.split(command)

    def test_both_paths_are_converted_for_wsl(self):
        output = self.root / "ramdisk.cpio"
        output.write_bytes(b"x" * 200)
        result, command = self._pack("C:/art-res/bin-amd64/busybox", str(output))
        self.assertTrue(result)
        tokens = self._tokens(command)
        self.assertIn("/mnt/c/art-res/bin-amd64/busybox", tokens)
        self.assertIn(windows_to_wsl(str(output)), tokens)
        leftovers = [token for token in tokens if re.match(r"[A-Za-z]:[/\\]", token)]
        self.assertEqual(leftovers, [], command)

    def test_command_survives_the_toolchain_conversion(self):
        output = self.root / "ramdisk.cpio"
        output.write_bytes(b"x" * 200)
        _, command = self._pack("C:/art-res/bin-amd64/busybox", str(output))
        # run(shell=True) converts the finished string again; already converted
        # paths must come out unchanged.
        self.assertEqual(windows_to_wsl(command), command)

    def test_paths_with_spaces_stay_single_arguments(self):
        spaced = self.root / "my roms"
        spaced.mkdir()
        output = spaced / "ramdisk.cpio"
        output.write_bytes(b"x" * 200)
        result, command = self._pack("C:/Program Files/art/busybox", str(output))
        self.assertTrue(result)
        tokens = self._tokens(command)
        self.assertIn("/mnt/c/Program Files/art/busybox", tokens)
        self.assertIn(windows_to_wsl(str(output)), tokens)

    def test_pipeline_and_cpio_options_are_preserved(self):
        output = self.root / "ramdisk.cpio"
        output.write_bytes(b"x" * 200)
        _, command = self._pack("C:/art-res/bin-amd64/busybox", str(output))
        self.assertIn("find . -mindepth 1", command)
        for option in ("cpio", "-o", "-H", "newc", "-R", "0:0", "-F"):
            self.assertIn(option, self._tokens(command))


if __name__ == "__main__":
    unittest.main()
