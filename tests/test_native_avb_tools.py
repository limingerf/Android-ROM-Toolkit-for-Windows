"""Guard native AVB dispatch and verified, atomic OTA-tool provisioning."""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from Scripts.Platform.runtime import (
    BUILTIN_AVBTOOL, Toolchain, ToolchainError, bootstrap_native_avbroot,
)


class NativeAvbDispatchTests(unittest.TestCase):
    def test_builtin_avbtool_dispatches_without_python_or_gui_in_frozen_app(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chain = Toolchain("native", root, root)
            with patch("Scripts.Platform.runtime.shutil.which", return_value=None):
                self.assertEqual(chain.resolve("avbtool"), BUILTIN_AVBTOOL)
                source_command = chain.command(["avbtool", "version"])
                self.assertEqual(source_command[0], sys.executable)
                self.assertEqual(Path(source_command[1]).name, "main.py")
                self.assertEqual(source_command[2:], ["--avbtool", "version"])
                with patch.object(sys, "frozen", True, create=True):
                    self.assertEqual(chain.command(["avbtool", "version"]),
                                     [sys.executable, "--avbtool", "version"])
                    external = root / "avbtool.py"
                    external.write_text("raise RuntimeError('must not run')", encoding="utf-8")
                    self.assertEqual(chain.resolve("avbtool"), BUILTIN_AVBTOOL)
                    self.assertEqual(chain.command([external, "version"], bundled=False),
                                     [sys.executable, "--avbtool", "version"])

    def test_diagnostics_do_not_download_but_requested_missing_avbroot_does(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "art-res" / "bin-win-amd64"
            target.mkdir(parents=True)
            chain = Toolchain("native", root, target)

            def install(*args, **kwargs):
                executable = target / "avbroot.exe"
                executable.write_bytes(b"verified fixture")
                return {"downloaded": [executable.name], "directory": str(target)}

            with patch("Scripts.Platform.runtime.shutil.which", return_value=None), \
                    patch("Scripts.Platform.runtime.bootstrap_native_avbroot", side_effect=install) as bootstrap:
                self.assertIn("avbroot", chain.diagnostics()["missing"])
                bootstrap.assert_not_called()
                self.assertEqual(chain.command(["avbroot", "--version"]),
                                 [str(target / "avbroot.exe"), "--version"])
                bootstrap.assert_called_once()

    def test_wsl_remains_explicit_and_uses_its_existing_linux_avbtool(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "avbtool").write_text("Linux fixture", encoding="ascii")
            chain = Toolchain("wsl", root, root, (root,), "wsl.exe", root)
            command = chain.command(["avbtool", "version"])
            self.assertEqual(command[0], "wsl.exe")
            self.assertEqual(command[-1], "version")
            self.assertNotIn("--avbtool", command)
            self.assertNotIn(BUILTIN_AVBTOOL, command)


@unittest.skipUnless(os.name == "nt", "Windows native executable provisioning")
class AvbrootProvisioningTests(unittest.TestCase):
    @staticmethod
    def archive():
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("avbroot.exe", b"Windows-executable fixture")
            archive.writestr("LICENSE", b"GPL-3.0")
            archive.writestr("README.md", b"Official tool usage")
            archive.writestr("../not-extracted.txt", b"ignored entry")
        return stream.getvalue()

    @staticmethod
    def release(archive, digest=None):
        filename = "avbroot-3.34.1-x86_64-pc-windows-msvc.zip"
        return json.dumps({"tag_name": "v3.34.1", "assets": [{
            "name": filename,
            "digest": digest if digest is not None else "sha256:" + hashlib.sha256(archive).hexdigest(),
            "browser_download_url": "https://github.com/chenxiaolong/avbroot/releases/download/v3.34.1/" + filename,
        }]}).encode()

    def test_checked_archive_and_executable_are_installed_and_temp_is_removed(self):
        archive = self.archive()
        responses = [io.BytesIO(self.release(archive)), io.BytesIO(archive)]
        with tempfile.TemporaryDirectory() as directory, \
                patch("Scripts.Platform.runtime._architecture", return_value="amd64"), \
                patch("Scripts.Platform.runtime.urllib.request.urlopen", side_effect=responses) as download, \
                patch("Scripts.Platform.runtime._native_avbroot_version", return_value="3.34.1"):
            result = bootstrap_native_avbroot(directory)
            target = Path(result["directory"])
            self.assertEqual((target / "avbroot.exe").read_bytes(), b"Windows-executable fixture")
            self.assertEqual((target / "licenses" / "avbroot" / "LICENSE").read_bytes(), b"GPL-3.0")
            self.assertFalse((target / "not-extracted.txt").exists())
            self.assertFalse(list(target.glob(".avbroot-download-*")))
            self.assertEqual(result["version"], "3.34.1")
            self.assertTrue(all(call.kwargs["timeout"] == 60 for call in download.call_args_list))

    def test_hash_failure_preserves_old_executable_and_cleans_staging(self):
        archive = self.archive()
        responses = [io.BytesIO(self.release(archive, "sha256:" + "0" * 64)), io.BytesIO(archive)]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "art-res" / "bin-win-amd64"
            target.mkdir(parents=True)
            executable = target / "avbroot.exe"
            executable.write_bytes(b"old executable")
            with patch("Scripts.Platform.runtime._architecture", return_value="amd64"), \
                    patch("Scripts.Platform.runtime.urllib.request.urlopen", side_effect=responses), \
                    patch("Scripts.Platform.runtime._native_avbroot_version", side_effect=ToolchainError("old invalid")):
                with self.assertRaisesRegex(ToolchainError, "SHA256"):
                    bootstrap_native_avbroot(directory)
            self.assertEqual(executable.read_bytes(), b"old executable")
            self.assertFalse(list(target.glob(".avbroot-download-*")))

    def test_missing_official_digest_refuses_download(self):
        archive = self.archive()
        with tempfile.TemporaryDirectory() as directory, \
                patch("Scripts.Platform.runtime._architecture", return_value="amd64"), \
                patch("Scripts.Platform.runtime.urllib.request.urlopen",
                      return_value=io.BytesIO(self.release(archive, ""))) as download:
            with self.assertRaisesRegex(ToolchainError, "SHA256"):
                bootstrap_native_avbroot(directory)
            download.assert_called_once()
            self.assertFalse((Path(directory) / "art-res" / "bin-win-amd64" / "avbroot.exe").exists())

    def test_failed_version_check_does_not_install_checked_but_unusable_binary(self):
        archive = self.archive()
        with tempfile.TemporaryDirectory() as directory, \
                patch("Scripts.Platform.runtime._architecture", return_value="amd64"), \
                patch("Scripts.Platform.runtime.urllib.request.urlopen",
                      side_effect=[io.BytesIO(self.release(archive)), io.BytesIO(archive)]), \
                patch("Scripts.Platform.runtime._native_avbroot_version", side_effect=ToolchainError("DLL missing")):
            with self.assertRaisesRegex(ToolchainError, "DLL missing"):
                bootstrap_native_avbroot(directory)
            target = Path(directory) / "art-res" / "bin-win-amd64"
            self.assertFalse((target / "avbroot.exe").exists())
            self.assertFalse(list(target.glob(".avbroot-download-*")))


if __name__ == "__main__":
    unittest.main()
