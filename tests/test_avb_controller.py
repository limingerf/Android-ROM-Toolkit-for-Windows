"""AVB sizing, noninteractive keys, and official/fork OTA command contracts."""
from pathlib import Path
import os
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from Scripts.application import ArtController


def captured(output="", ok=True):
    return {"ok": ok, "code": 0 if ok else 1, "output": output}


class AvbControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.controller = ArtController(self.root)
        self.image = self.root / "boot.img"
        self.image.write_bytes(b"b" * 4096)

    def test_no_key_uses_unsigned_dynamic_hash_footer(self):
        calls = []

        def capture(tool, args, **_):
            calls.append(args)
            return captured("--dynamic_partition_size" if "--help" in args else "")

        with patch.object(self.controller, "_tool_capture", side_effect=capture):
            self.controller.avb_add_footer(str(self.image))
        args = calls[-1]
        self.assertEqual(args[args.index("--algorithm") + 1], "NONE")
        self.assertIn("--dynamic_partition_size", args)
        self.assertNotIn("--partition_size", args)
        self.assertNotIn("--key", args)

    def test_builtin_key_uses_its_algorithm_and_explicit_size(self):
        key = self.root / "test.key"
        key.write_text("fixture", encoding="utf-8")
        with patch("Scripts.signing.resolve_avb_key", return_value=key) as resolve, \
                patch("Scripts.signing.avb_algorithm_for_key", return_value="SHA256_RSA2048"), \
                patch.object(self.controller, "_tool_capture", return_value=captured()) as capture:
            self.controller.avb_add_footer(str(self.image), key="builtin:rsa2048", partition_size=131072)
        resolve.assert_called_once_with("builtin:rsa2048")
        args = capture.call_args.args[1]
        self.assertEqual(args[args.index("--algorithm") + 1], "SHA256_RSA2048")
        self.assertEqual(args[args.index("--partition_size") + 1], 131072)
        self.assertEqual(args[args.index("--key") + 1], key)

    def test_signed_algorithm_without_key_fails_before_output(self):
        with self.assertRaisesRegex(ValueError, "必须选择 AVB 密钥"):
            self.controller.avb_add_footer(str(self.image), algorithm="SHA256_RSA4096")
        self.assertFalse(self.image.with_name("boot_signed.img").exists())

    def test_hashtree_retries_reserve_for_sparse_logical_size(self):
        self.image.write_bytes(struct.pack("<IHHHHIIII", 0xed26ff3a, 1, 0, 28, 12, 4096, 1024, 0, 0))
        logical_size = 4096 * 1024
        sizes = []

        def capture(tool, args, **_):
            if "--calc_max_image_size" in args:
                sizes.append(int(args[args.index("--partition_size") + 1]))
                return captured(str(logical_size - 4096 if len(sizes) == 1 else logical_size))
            return captured()

        with patch.object(self.controller, "_tool_capture", side_effect=capture):
            self.controller.avb_add_footer(str(self.image), kind="hashtree")
        self.assertEqual(len(sizes), 2)
        self.assertGreater(sizes[0], logical_size)
        self.assertGreater(sizes[1], sizes[0])

    def test_failed_tool_removes_generated_image(self):
        with patch.object(self.controller, "_tool_capture", return_value=captured("native tool failed", False)):
            with self.assertRaisesRegex(RuntimeError, "native tool failed"):
                self.controller.avb_add_footer(str(self.image), partition_size=131072)
        self.assertFalse(self.image.with_name("boot_signed.img").exists())

    def test_launcher_failure_removes_generated_image(self):
        with patch.object(self.controller, "_tool_capture", side_effect=FileNotFoundError("avbtool unavailable")):
            with self.assertRaises(FileNotFoundError):
                self.controller.avb_add_footer(str(self.image), partition_size=131072)
        self.assertFalse(self.image.with_name("boot_signed.img").exists())

    def test_failed_add_and_erase_preserve_previous_outputs(self):
        for operation, name in (("add", "boot_signed.img"), ("erase", "boot_unsign.img")):
            with self.subTest(operation=operation):
                target = self.root / name
                target.write_bytes(b"previous successful image")
                with patch.object(self.controller, "_tool_capture", return_value=captured("native tool failed", False)):
                    with self.assertRaisesRegex(RuntimeError, "native tool failed"):
                        if operation == "add":
                            self.controller.avb_add_footer(str(self.image), partition_size=131072)
                        else:
                            self.controller.avb_erase_footer(str(self.image))
                self.assertEqual(target.read_bytes(), b"previous successful image")
                self.assertFalse(list(self.root.glob(".art-avb-*")))

    def test_launcher_failure_preserves_previous_output(self):
        target = self.root / "boot_signed.img"
        target.write_bytes(b"previous successful image")
        with patch.object(self.controller, "_tool_capture", side_effect=OSError("failed to start")):
            with self.assertRaises(OSError):
                self.controller.avb_add_footer(str(self.image), partition_size=131072)
        self.assertEqual(target.read_bytes(), b"previous successful image")
        self.assertFalse(list(self.root.glob(".art-avb-*")))

    @unittest.skipUnless(os.name == "nt", "Windows-native signing integration")
    def test_real_encrypted_custom_key_sign_verify_and_erase(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key_path = self.root / "encrypted.key"
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key_path.write_bytes(private.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"fixture-only-password"),
        ))
        (self.root / "passphrase.txt").write_text("fixture-only-password", encoding="utf-8")
        source_bytes = self.image.read_bytes()
        with patch.dict(os.environ, {"ART_BACKEND": "native"}):
            controller = ArtController(self.root)
            signed = controller.avb_add_footer(str(self.image), key=str(key_path))
            self.assertTrue(controller.avb_verify(signed["output"])["verified"])
            restored = controller.avb_erase_footer(signed["output"])
            self.assertEqual(Path(restored["output"]).read_bytes(), source_bytes)
            self.assertEqual(self.image.read_bytes(), source_bytes)
            with Path(signed["output"]).open("r+b") as image:
                image.write(b"tampered")
            self.assertFalse(controller.avb_verify(signed["output"])["verified"])


class OtaControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.controller = ArtController(self.root)
        self.controller.create_project("test")
        self.layout = self.controller._layout("DNA_test")
        self.controller.ota_status("DNA_test")
        (self.layout.ota_stockzip_dir / "ota.zip").write_bytes(b"fixture")
        self.controller.ota_select_zip("DNA_test", "ota.zip")
        for name in ("avb.key", "ota.key", "ota.crt"):
            (self.layout.ota_signkey_dir / name).write_text("fixture", encoding="utf-8")
        (self.layout.ota_inputimg_dir / "boot.img").write_bytes(b"fixture")

    def test_official_avbroot_replaces_existing_partition_with_both_keys(self):
        calls = []

        def capture(tool, args, **_):
            calls.append(args)
            if "--help" in args:
                return captured("--key-avb FILE --key-ota FILE --replace PARTITION FILE")
            if args[:2] == ["ota", "list"]:
                return captured("boot\nsystem\n")
            Path(args[args.index("--output") + 1]).write_bytes(b"signed ZIP fixture")
            return captured()

        with patch.object(self.controller, "_tool_capture", side_effect=capture):
            self.controller.ota_patch("DNA_test")
        args = calls[-1]
        self.assertIn("--key-avb", args)
        self.assertIn("--key-ota", args)
        self.assertIn("--replace", args)
        self.assertNotIn("--add-partition", args)

    def test_official_avbroot_rejects_disable_before_patch(self):
        with patch.object(self.controller, "_tool_capture", return_value=captured("--key-avb --replace")) as capture:
            with self.assertRaisesRegex(ValueError, "不支持禁用 AVB"):
                self.controller.ota_patch("DNA_test", disable_avb=True)
        self.assertEqual(capture.call_count, 1)

    def test_official_avbroot_rejects_new_partition(self):
        with patch.object(self.controller, "_tool_capture", side_effect=[captured("--key-avb --replace"), captured("system")]):
            with self.assertRaisesRegex(ValueError, "不支持添加 OTA 分区：boot"):
                self.controller.ota_patch("DNA_test")

    def test_fork_extensions_are_preserved(self):
        def simulate(tool, args, **_):
            if "--help" in args:
                return captured("--replace --add-partition --disable-avb")
            if args[:2] == ["ota", "list"]:
                return captured("system")
            Path(args[args.index("--output") + 1]).write_bytes(b"signed ZIP fixture")
            return captured()

        with patch.object(self.controller, "_tool_capture", side_effect=simulate) as capture:
            self.controller.ota_patch("DNA_test", disable_avb=True)
        args = capture.call_args.args[1]
        self.assertIn("--add-partition", args)
        self.assertIn("--disable-avb", args)
        self.assertNotIn("--key-avb", args)

    def test_failed_listing_never_classifies_all_images_as_additions(self):
        with patch.object(self.controller, "_tool_capture", side_effect=[
            captured("--replace --add-partition"), captured("broken source OTA", False),
        ]) as capture:
            with self.assertRaisesRegex(RuntimeError, "broken source OTA"):
                self.controller.ota_patch("DNA_test")
        self.assertEqual(capture.call_count, 2)

    def test_failed_patch_preserves_previous_signed_zip(self):
        previous = self.layout.ota_work_dir / "ota_signed.zip"
        previous.write_bytes(b"previous successful ZIP")

        def simulate(tool, args, **_):
            if "--help" in args:
                return captured("--replace --key-avb")
            if args[:2] == ["ota", "list"]:
                return captured("boot")
            Path(args[args.index("--output") + 1]).write_bytes(b"partial failed ZIP")
            return captured("native patch failed", False)

        with patch.object(self.controller, "_tool_capture", side_effect=simulate):
            with self.assertRaisesRegex(RuntimeError, "native patch failed"):
                self.controller.ota_patch("DNA_test")
        self.assertEqual(previous.read_bytes(), b"previous successful ZIP")
        self.assertFalse(list(self.layout.ota_work_dir.glob(".art-ota-*")))

    def test_zero_exit_without_zip_is_a_failure_and_preserves_old_zip(self):
        previous = self.layout.ota_work_dir / "ota_signed.zip"
        previous.write_bytes(b"previous successful ZIP")
        with patch.object(self.controller, "_tool_capture", side_effect=[
            captured("--replace --key-avb"), captured("boot"), captured(),
        ]):
            with self.assertRaisesRegex(RuntimeError, "未生成有效 ZIP"):
                self.controller.ota_patch("DNA_test")
        self.assertEqual(previous.read_bytes(), b"previous successful ZIP")
        self.assertFalse(list(self.layout.ota_work_dir.glob(".art-ota-*")))

    def test_empty_password_is_explicit_and_removed_after_key_generation(self):
        seen = []

        def capture(tool, args, **_):
            password = args[args.index("--pass-file") + 1]
            seen.append(password.read_text(encoding="utf-8"))
            return captured()

        with patch.object(self.controller, "_tool_capture", side_effect=capture):
            self.controller.ota_generate_keys("DNA_test")
        self.assertEqual(seen, ["", "", "", ""])
        self.assertFalse((self.layout.ota_signkey_dir / "passphrase.txt").exists())


class NativeCliTests(unittest.TestCase):
    def test_payload_command_rejects_unsupported_disable_flag(self):
        from Scripts.ReMake import payload
        toolchain = Mock()
        toolchain.capture.return_value = subprocess.CompletedProcess([], 0, "--key-avb --replace", "")
        layout = Mock(ota_work_dir=Path("OTA_WORK"))
        with patch.object(payload.V, "toolchain", toolchain), patch.object(payload.V, "layout", layout):
            with self.assertRaisesRegex(ValueError, "不支持禁用 AVB"):
                payload._build_patch_cmd(Path("ota.zip"), Path("sign-key"), [], [], [], disable_avb=True)

    def test_payload_list_uses_resolved_toolchain_and_reports_failure(self):
        from Scripts.ReMake import payload
        toolchain = Mock()
        toolchain.capture.return_value = subprocess.CompletedProcess([], 1, "", "invalid OTA")
        with patch.object(payload.V, "toolchain", toolchain):
            with self.assertRaisesRegex(RuntimeError, "invalid OTA"):
                payload._get_ota_parts(Path("ota.zip"))
        toolchain.capture.assert_called_once_with(
            ["avbroot", "ota", "list", "--input", "ota.zip"], capture_output=True, text=True,
        )

    def test_avb_cli_uses_shared_decoder_and_hidden_native_process(self):
        from Scripts.Primary import VBMetaTools
        toolchain = Mock()
        with patch.object(VBMetaTools.V, "toolchain", toolchain):
            VBMetaTools._run_capture(["version"])
        toolchain.capture.assert_called_once_with(["avbtool", "version"], capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
