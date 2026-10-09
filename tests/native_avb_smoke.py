"""Real Windows AVB/OTA smoke checks with no developer tools on PATH.

Run: python tests/native_avb_smoke.py [path/to/art.exe]
The optional executable is checked in addition to the source controller.
All images and generated keys live in an isolated temporary Chinese path.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))

from Scripts.application import ArtController
from Scripts.Platform.runtime import detect_toolchain, process_options


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def restricted_environment(resource_root):
    env = os.environ.copy()
    system = Path(env.get("SystemRoot", r"C:\Windows"))
    env.update({
        "PATH": str(system / "System32"),
        "ART_BACKEND": "native",
        "ART_WINDOWS_TOOLS": str(resource_root / "art-res" / "bin-win-amd64"),
        "PYTHONUTF8": "1",
    })
    for key in ("PYTHONHOME", "PYTHONPATH", "QT_PLUGIN_PATH", "QML2_IMPORT_PATH"):
        env.pop(key, None)
    return env


class NativeTools:
    def __init__(self, resource_root, executable=None):
        self.toolchain = detect_toolchain(resource_root)
        self.executable = executable
        check(self.toolchain.mode == "native", "Native AVB checks require Windows")
        avbroot = self.toolchain.resolve("avbroot", required=False)
        check(avbroot and Path(avbroot).resolve().is_relative_to(resource_root),
              "avbroot.exe must be shipped in the tested tool root; no download or PATH fallback is allowed")
        if executable is None:
            check("--avbtool" in self.toolchain.command(["avbtool", "version"]),
                  "Source check must use the bundled AVB adapter")

    def avb(self, *args, succeeds=True):
        if self.executable:
            command = [str(self.executable), "--avbtool", *map(str, args)]
            result = subprocess.run(command, capture_output=True, text=True,
                                    encoding="utf-8", errors="strict", timeout=90,
                                    stdin=subprocess.DEVNULL, **process_options())
        else:
            result = self.toolchain.capture(["avbtool", *map(str, args)],
                                            capture_output=True, text=True, timeout=90,
                                            stdin=subprocess.DEVNULL)
        output = (result.stdout or "") + (result.stderr or "")
        check((result.returncode == 0) == succeeds,
              f"AVB command had unexpected exit code {result.returncode}: {args[0]}\n{output}")
        check("\ufffd" not in output and "\x00" not in output,
              "AVB command returned corrupt text")
        return output


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def altered_copy(source, output, offset=4096):
    shutil.copy2(source, output)
    with output.open("r+b") as image:
        image.seek(offset)
        original = image.read(1)
        image.seek(offset)
        image.write(bytes([original[0] ^ 1]))


def altered_signature_copy(source, output):
    from Scripts.vendor.avbtool import AvbFooter, AvbVBMetaHeader

    with source.open("rb") as image:
        image.seek(-AvbFooter.SIZE, os.SEEK_END)
        footer = AvbFooter(image.read(AvbFooter.SIZE))
        image.seek(footer.vbmeta_offset)
        header = AvbVBMetaHeader(image.read(AvbVBMetaHeader.SIZE))
    check(header.signature_size > 0, "Signed fixture has no authentication signature")
    altered_copy(source, output, footer.vbmeta_offset + AvbVBMetaHeader.SIZE + header.signature_offset)


def verify_source_footer(controller, tools, original, output, *, bits, kind):
    before = digest(original)
    result = controller.avb_add_footer(str(original), kind=kind,
                                       key=f"builtin:rsa{bits}",
                                       partition_name="system", output=str(output))
    check(result["ok"] and output.is_file(), "Source controller did not produce a signed image")
    if kind == "hashtree":
        check("--do_not_generate_fec" not in result["command"],
              "Native hashtree should use the bundled FEC implementation")
    info = controller.avb_info(str(output))["output"]
    check(f"SHA256_RSA{bits}" in info, "Signed image uses an unexpected RSA algorithm")
    check(("Hashtree descriptor:" if kind == "hashtree" else "Hash descriptor:") in info,
          "Signed image is missing its expected descriptor")
    verification = controller.avb_verify(str(output))
    check(verification["verified"], f"Source signature verification failed\n{verification['output']}")
    tools.avb("verify_image", "--image", output, "--key", f"builtin:rsa{bits}")
    tampered = output.with_name(output.stem + "_tampered.img")
    altered_copy(output, tampered)
    check(not controller.avb_verify(str(tampered))["verified"],
          "Modified source image incorrectly passed verification")
    signature_tampered = output.with_name(output.stem + "_bad_signature.img")
    altered_signature_copy(output, signature_tampered)
    check(not controller.avb_verify(str(signature_tampered))["verified"],
          "Modified source RSA authentication signature incorrectly passed verification")
    wrong_key = "builtin:rsa4096" if bits == 2048 else "builtin:rsa2048"
    tools.avb("verify_image", "--image", output, "--key", wrong_key, succeeds=False)
    erased = output.with_name(output.stem + "_erased.img")
    controller.avb_erase_footer(str(output), str(erased))
    check(erased.stat().st_size == original.stat().st_size and digest(erased) == before,
          "Erasing the source footer did not restore the exact original image")
    check(digest(original) == before, "Source AVB operation modified its input image")
    print(f"Source {kind} RSA{bits}: PASS (sign, info, verify, data/signature/wrong-key rejection, exact erase)", flush=True)


def verify_frozen_footer(tools, original, output, *, bits, kind):
    before = digest(original)
    partition_size = 2 * 1024 * 1024
    command = "add_hash_footer" if kind == "hash" else "add_hashtree_footer"
    fec_args = []
    maximum = tools.avb(command, "--partition_size", partition_size,
                        "--calc_max_image_size", *fec_args)
    maximum_values = [int(line.strip()) for line in maximum.splitlines() if line.strip().isdigit()]
    check(maximum_values and maximum_values[-1] >= original.stat().st_size,
          "Frozen footer has insufficient image/hash-tree reserve")
    shutil.copy2(original, output)
    # Builtin markers intentionally avoid source PEM paths. This only passes
    # when the two key assets and the RSA adapter were collected into the EXE.
    tools.avb(command, "--image", output, "--partition_name", "system",
              "--partition_size", partition_size, "--algorithm", f"SHA256_RSA{bits}",
              "--key", f"builtin:rsa{bits}", *fec_args)
    info = tools.avb("info_image", "--image", output)
    check(f"SHA256_RSA{bits}" in info, "Frozen image uses an unexpected RSA algorithm")
    check(("Hashtree descriptor:" if kind == "hashtree" else "Hash descriptor:") in info,
          "Frozen image is missing its expected descriptor")
    if kind == "hashtree":
        check("FEC num roots:" in info and "FEC size:              0 bytes" not in info,
              "Frozen hashtree image did not contain embedded FEC data")
    tools.avb("verify_image", "--image", output, "--key", f"builtin:rsa{bits}")
    tampered = output.with_name(output.stem + "_tampered.img")
    altered_copy(output, tampered)
    tools.avb("verify_image", "--image", tampered, succeeds=False)
    signature_tampered = output.with_name(output.stem + "_bad_signature.img")
    altered_signature_copy(output, signature_tampered)
    tools.avb("verify_image", "--image", signature_tampered, succeeds=False)
    wrong_key = "builtin:rsa4096" if bits == 2048 else "builtin:rsa2048"
    tools.avb("verify_image", "--image", output, "--key", wrong_key, succeeds=False)
    erased = output.with_name(output.stem + "_erased.img")
    shutil.copy2(output, erased)
    tools.avb("erase_footer", "--image", erased)
    check(erased.stat().st_size == original.stat().st_size and digest(erased) == before,
          "Erasing the frozen footer did not restore the exact original image")
    check(digest(original) == before, "Frozen AVB operation modified its input image")
    print(f"Frozen {kind} RSA{bits}: PASS (embedded key, reserve, sign, verify, data/signature/wrong-key rejection, exact erase)", flush=True)


def verify_ota_keys(controller, tools, workspace, label):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    version = tools.toolchain.capture(["avbroot", "--version"], capture_output=True,
                                      text=True, timeout=30, stdin=subprocess.DEVNULL)
    check(version.returncode == 0 and "avbroot" in version.stdout.lower(),
          f"Bundled native avbroot did not launch: {version.stderr}")
    project = controller.create_project("native_avb_smoke")["name"]
    result = controller.ota_generate_keys(project, passphrase="")
    check(all(result["keys"].get(name) for name in ("avb.key", "ota.key", "avb_pkmd.bin", "ota.crt")),
          "OTA key generation did not produce all four key/certificate artifacts")
    directory = controller._layout(project).ota_signkey_dir
    check(not (directory / "passphrase.txt").exists(), "Empty password file was not cleaned up")
    check(len(result["results"]) == 4 and all(item["ok"] for item in result["results"]),
          "OTA key generation reported an unsuccessful native command")
    check(all("--pass-file" in item["command"] for item in result["results"]),
          "OTA generation must specify an empty password file rather than prompt")
    avb = serialization.load_pem_private_key((directory / "avb.key").read_bytes(), password=None)
    ota = serialization.load_pem_private_key((directory / "ota.key").read_bytes(), password=None)
    check(avb.key_size == 4096 and ota.key_size == 4096, "Generated RSA keys have unexpected bit sizes")
    certificate = x509.load_pem_x509_certificate((directory / "ota.crt").read_bytes())
    check(certificate.public_key().public_numbers() == ota.public_key().public_numbers(),
          "OTA certificate does not match its generated private key")
    encoded = (directory / "avb_pkmd.bin").read_bytes()
    check(len(encoded) == 1032 and struct.unpack_from("!I", encoded)[0] == 4096,
          "AVB public key was not encoded as RSA4096")
    regenerated = workspace / "重新编码公钥.bin"
    tools.avb("extract_public_key", "--key", directory / "avb.key", "--output", regenerated)
    check(regenerated.read_bytes() == encoded, "Native avbtool and avbroot disagree on AVB public key encoding")
    print(f"{label} native avbroot: PASS ({version.stdout.strip()}, empty-password RSA keys, matching certificate/public key)", flush=True)


def run_case(resource_root, workspace, executable):
    native_environment = restricted_environment(resource_root)
    os.environ.clear()
    os.environ.update(native_environment)
    workspace.mkdir(parents=True)
    tools = NativeTools(resource_root, executable)
    controller = ArtController(workspace)
    controller.toolchain = tools.toolchain
    original = workspace / "原始系统.img"
    data = (b"Android ROM Toolkit native AVB verification\n" * 32768)[:1024 * 1024]
    original.write_bytes(data)
    check(original.stat().st_size == 1024 * 1024, "Smoke image fixture must have a full MiB")
    check("avbtool" in tools.avb("version").lower(), "Native AVB version output is missing")
    for bits in (2048, 4096):
        output = workspace / f"hash_rsa{bits}.img"
        if executable:
            verify_frozen_footer(tools, original, output, bits=bits, kind="hash")
        else:
            verify_source_footer(controller, tools, original, output, bits=bits, kind="hash")
    output = workspace / "hashtree_rsa4096.img"
    if executable:
        verify_frozen_footer(tools, original, output, bits=4096, kind="hashtree")
    else:
        verify_source_footer(controller, tools, original, output, bits=4096, kind="hashtree")
        failed = workspace / "签名失败不应保留.img"
        before = digest(original)
        try:
            controller.avb_add_footer(str(original), key="builtin:rsa2048",
                                      algorithm="SHA256_RSA4096", output=str(failed))
        except RuntimeError:
            pass
        else:
            raise AssertionError("Mismatched RSA key/algorithm unexpectedly signed an image")
        check(not failed.exists() and digest(original) == before,
              "Failed signing left a partial artifact or modified its input")
        print("Source signing failure cleanup: PASS (partial target removed, input preserved)", flush=True)
    verify_ota_keys(controller, tools, workspace, "Frozen tools" if executable else "Source")


def main():
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", nargs="?", type=Path)
    parser.add_argument("--case-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--workspace", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This integration check requires Windows")
    executable = args.executable.resolve() if args.executable else None
    if executable:
        check(executable.is_file(), f"Frozen executable does not exist: {executable}")
    if args.case_root:
        run_case(args.case_root.resolve(), args.workspace.resolve(), executable)
        return
    with tempfile.TemporaryDirectory(prefix="art-native-avb-") as temporary:
        cases = [("源码原生验证", REPOSITORY, None)]
        if executable:
            cases.append(("单文件原生验证", executable.parent, executable))
        for label, resource_root, frozen in cases:
            workspace = Path(temporary) / label
            command = [sys.executable, str(Path(__file__).resolve()),
                       "--case-root", str(resource_root), "--workspace", str(workspace)]
            if frozen:
                command.append(str(frozen))
            result = subprocess.run(command, env=restricted_environment(resource_root),
                                    capture_output=True, text=True, encoding="utf-8",
                                    errors="strict", stdin=subprocess.DEVNULL,
                                    timeout=300, **process_options())
            if result.stdout:
                print(result.stdout.rstrip(), flush=True)
            check(result.returncode == 0, f"{label} failed (exit {result.returncode})\n{result.stderr}")
    print("Native AVB/OTA smoke checks: PASS (System32-only PATH, native backend, temporary files cleaned)", flush=True)


if __name__ == "__main__":
    main()
