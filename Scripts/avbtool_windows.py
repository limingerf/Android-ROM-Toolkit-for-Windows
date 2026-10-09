"""AOSP avbtool with bundled RSA support and Windows-safe helper files.

The upstream module remains unchanged. This adapter replaces OpenSSL calls
with cryptography so the frozen EXE needs no installed Python or OpenSSL.
"""
from __future__ import annotations

import hashlib
import hmac
import builtins
import copy
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import shutil

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from Scripts.signing import load_rsa_key, resolve_avb_key
from Scripts.vendor import avbtool as upstream


_password = None
_hash_verify = upstream.AvbHashDescriptor.verify
_hashtree_verify = upstream.AvbHashtreeDescriptor.verify


class _RandomSource:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, size):
        return os.urandom(size)


def _platform_open(path, *args, **kwargs):
    # Two upstream salt-generation paths use a POSIX random device. Keep the
    # vendored source intact while using the Windows system CSPRNG.
    if path == "/dev/urandom":
        return _RandomSource()
    return builtins.open(path, *args, **kwargs)


class _Avb(upstream.Avb):
    def _parse_image(self, image):
        result = super()._parse_image(image)
        footer, _, descriptors, _ = result
        if footer:
            # A signed partition may be renamed to *_signed.img by the GUI.
            # Upstream resolves its descriptor to partition_name.img instead,
            # potentially verifying the unmodified input beside that output.
            # Identify the sole descriptor for data preceding this footer.
            matches = []
            for descriptor in descriptors:
                if isinstance(descriptor, upstream.AvbHashDescriptor):
                    if descriptor.image_size == footer.original_image_size:
                        matches.append(descriptor)
                elif isinstance(descriptor, upstream.AvbHashtreeDescriptor):
                    size = upstream.round_to_multiple(footer.original_image_size, descriptor.data_block_size)
                    if descriptor.image_size == size and descriptor.tree_offset == size:
                        matches.append(descriptor)
            if len(matches) == 1:
                matches[0]._art_embedded_image = True
        return result


def _descriptor_verify(descriptor, *args):
    original = _hash_verify if isinstance(descriptor, upstream.AvbHashDescriptor) else _hashtree_verify
    if getattr(descriptor, "_art_embedded_image", False):
        descriptor = copy.copy(descriptor)
        descriptor.partition_name = ""
        # Upstream assumes a new ImageHandler starts at byte zero. The
        # containing handler was just used to read the vbmeta structure.
        args[3].seek(0)
    return original(descriptor, *args)


class _RsaKey(upstream.RSAPublicKey):
    def __init__(self, key_path):
        try:
            self.key_path = resolve_avb_key(str(key_path))
            self.key = load_rsa_key(self.key_path, _password)
        except (OSError, ValueError) as error:
            raise upstream.AvbError(str(error)) from error
        public = self.key.public_key() if isinstance(self.key, rsa.RSAPrivateKey) else self.key
        numbers = public.public_numbers()
        self.exponent, self.modulus = numbers.e, numbers.n
        self.num_bits = public.key_size

    def sign(self, algorithm_name, data_to_sign, signing_helper=None,
             signing_helper_with_files=None):
        algorithm = upstream.ALGORITHMS.get(algorithm_name)
        if not algorithm or algorithm.signature_num_bytes * 8 != self.num_bits:
            raise upstream.AvbError("RSA 密钥位数与 AVB 签名算法不匹配")
        if signing_helper or signing_helper_with_files:
            from Scripts.Platform.runtime import process_options
            raw = algorithm.padding + hashlib.new(algorithm.hash_name, data_to_sign).digest()
            if signing_helper_with_files:
                # External processes cannot reopen a live NamedTemporaryFile
                # on Windows. Close a file inside a private directory first.
                with tempfile.TemporaryDirectory(prefix=".avb-sign-") as temporary:
                    path = Path(temporary) / "signature.bin"
                    path.write_bytes(raw)
                    result = subprocess.run([signing_helper_with_files, algorithm_name,
                                             self.key_path, str(path)], capture_output=True,
                                            **process_options())
                    signature = path.read_bytes() if result.returncode == 0 else b""
            else:
                result = subprocess.run([signing_helper, algorithm_name, self.key_path],
                                        input=raw, capture_output=True, **process_options())
                signature = result.stdout
            if result.returncode or len(signature) != algorithm.signature_num_bytes:
                raise upstream.AvbError("外部签名工具失败或返回了无效长度的签名")
            return signature
        if not isinstance(self.key, rsa.RSAPrivateKey):
            raise upstream.AvbError("签名需要 RSA 私钥")
        hash_algorithm = hashes.SHA256() if algorithm.hash_name == "sha256" else hashes.SHA512()
        return self.key.sign(bytes(data_to_sign), padding.PKCS1v15(), hash_algorithm)


def _verify_signature(header, blob):
    try:
        _, algorithm = upstream.lookup_algorithm_by_type(header.algorithm_type)
        if not algorithm.hash_name:
            return True
        aux = 256 + header.authentication_data_block_size
        signed_data = bytes(blob[:256] + blob[aux:aux + header.auxiliary_data_block_size])
        digest = bytes(blob[256 + header.hash_offset:256 + header.hash_offset + header.hash_size])
        if header.hash_size != algorithm.hash_num_bytes or not hmac.compare_digest(
                hashlib.new(algorithm.hash_name, signed_data).digest(), digest):
            return False
        encoded_key = blob[aux + header.public_key_offset:aux + header.public_key_offset + header.public_key_size]
        bits, = struct.unpack("!I", encoded_key[:4])
        if bits not in (2048, 4096, 8192) or len(encoded_key) != 8 + bits // 4:
            return False
        if bits != algorithm.signature_num_bytes * 8 or header.signature_size != algorithm.signature_num_bytes:
            return False
        modulus = int.from_bytes(encoded_key[8:8 + bits // 8], "big")
        public = rsa.RSAPublicNumbers(65537, modulus).public_key()
        signature = bytes(blob[256 + header.signature_offset:256 + header.signature_offset + header.signature_size])
        hash_algorithm = hashes.SHA256() if algorithm.hash_name == "sha256" else hashes.SHA512()
        public.verify(signature, signed_data, padding.PKCS1v15(), hash_algorithm)
        return True
    except (InvalidSignature, ValueError, struct.error):
        return False


_FEC_BLOCK_SIZE = 4096
_FEC_MAX_PARITY = 24


def _bundled_avbroot() -> Path | None:
    """Return the bundled avbroot executable used for FEC generation.

    AOSP's Python avbtool intentionally delegates Reed-Solomon generation to
    the ``fec`` helper.  Windows releases do not ship that legacy helper, but
    avbroot contains the same AOSP-compatible FEC implementation.  Calling
    the bundled binary keeps the format and interleaving identical to Android
    instead of shipping a second, subtly different implementation.
    """
    candidates = []
    env_path = os.environ.get("ART_AVBROOT")
    if env_path:
        candidates.append(Path(env_path))
    roots = [Path(__file__).resolve().parents[1]]
    if getattr(sys, "frozen", False):
        roots.insert(0, Path(sys.executable).resolve().parent)
    candidates.extend(
        resource_root / "art-res" / directory / "avbroot.exe"
        for resource_root in roots
        for directory in ("bin-win-amd64", "bin-win-arm64", "bin")
    )
    found = shutil.which("avbroot.exe") or shutil.which("avbroot")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _fec_params(image_size: int, num_roots: int) -> tuple[int, int]:
    """Return (rounds, raw FEC bytes) for AOSP's 4 KiB interleaving."""
    if image_size <= 0:
        raise upstream.AvbError("FEC 输入镜像不能为空")
    if not 2 <= int(num_roots) <= _FEC_MAX_PARITY:
        raise upstream.AvbError(f"FEC roots 必须在 2 到 {_FEC_MAX_PARITY} 之间")
    # Android partition images are block aligned.  avbtool's historical fec
    # helper rounds the final partial block up, so retain that behavior for
    # callers that use --calc_max_image_size with an unaligned size.
    blocks = (int(image_size) + _FEC_BLOCK_SIZE - 1) // _FEC_BLOCK_SIZE
    data_bytes = 255 - int(num_roots)
    rounds = (blocks + data_bytes - 1) // data_bytes
    return rounds, int(num_roots) * rounds * _FEC_BLOCK_SIZE


def _calc_fec_data_size(image_size, num_roots):
    """Replacement for ``fec --print-fec-size`` used by avbtool."""
    return _fec_params(int(image_size), int(num_roots))[1]


def _generate_fec_data(image_filename, num_roots):
    """Generate AOSP FEC payload bytes through bundled avbroot.

    ``avbroot fec generate`` writes the AOSP standalone header after the raw
    parity bytes.  avbtool needs only those raw bytes for embedding in an AVB
    image, so the header is validated and removed before returning.
    """
    executable = _bundled_avbroot()
    if executable is None:
        raise upstream.AvbError(
            "Windows 原生 FEC 工具缺失，请补齐 art-res/bin-win-amd64/avbroot.exe"
        )
    expected_size = _calc_fec_data_size(os.path.getsize(image_filename), num_roots)
    with tempfile.TemporaryDirectory(prefix=".art-fec-") as temporary:
        output = Path(temporary) / "image.fec"
        command = [str(executable), "fec", "generate", "--input", str(image_filename),
                   "--fec", str(output), "--parity", str(num_roots)]
        try:
            from Scripts.Platform.runtime import process_options
            result = subprocess.run(command, capture_output=True, **process_options())
        except OSError as error:
            raise upstream.AvbError(f"启动 Windows FEC 工具失败：{error}") from error
        if result.returncode != 0 or not output.is_file():
            detail = (result.stderr or result.stdout or b"").decode("utf-8", "replace").strip()
            raise upstream.AvbError(f"FEC 生成失败{(': ' + detail) if detail else ''}")
        blob = output.read_bytes()
    if len(blob) < _FEC_BLOCK_SIZE or len(blob) - _FEC_BLOCK_SIZE != expected_size:
        raise upstream.AvbError(
            f"FEC 工具返回长度无效：得到 {max(0, len(blob) - _FEC_BLOCK_SIZE)}，"
            f"预期 {expected_size}"
        )
    # Validate the avbroot/AOSP standalone footer before stripping its two
    # 4 KiB header copies.  The raw FEC bytes are exactly the prefix.
    footer = blob[-_FEC_BLOCK_SIZE:]
    if footer[:4] != struct.pack("<I", 0xFECFECFE):
        raise upstream.AvbError("FEC 工具返回了无法识别的 AOSP FEC 头")
    parity, fec_size = struct.unpack_from("<II", footer, 12)
    if parity != int(num_roots) or fec_size != expected_size:
        raise upstream.AvbError("FEC 工具返回的 roots/size 与请求不一致")
    return blob[:fec_size]


def main(argv=None) -> int:
    global _password
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        _password = None
        if "--pass-file" in args:
            index = args.index("--pass-file")
            if index + 1 == len(args):
                raise ValueError("--pass-file 缺少文件路径")
            _password = Path(args[index + 1]).read_bytes().rstrip(b"\r\n") or None
            del args[index:index + 2]
        # FileType key arguments require real paths before upstream parsing.
        for index, value in enumerate(args):
            if value in {"--key", "--authority_key", "--subject_key", "--root_authority_key", "--unlock_key"} and index + 1 < len(args):
                args[index + 1] = resolve_avb_key(args[index + 1])
        upstream.RSAPublicKey = _RsaKey
        upstream.Avb = _Avb
        upstream.AvbHashDescriptor.verify = _descriptor_verify
        upstream.AvbHashtreeDescriptor.verify = _descriptor_verify
        upstream.open = _platform_open
        upstream.verify_vbmeta_signature = _verify_signature
        upstream.calc_fec_data_size = _calc_fec_data_size
        upstream.generate_fec_data = _generate_fec_data
        upstream.AvbTool().run(["avbtool", *args])
        return 0
    except SystemExit as error:
        return int(error.code or 0)
    except (OSError, ValueError, upstream.AvbError) as error:
        print(f"avbtool: {error}", file=sys.stderr)
        return 1
    finally:
        _password = None


if __name__ == "__main__":
    raise SystemExit(main())
