"""Shared AVB test-key resolution and RSA validation for GUI, CLI and MCP."""
from pathlib import Path
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


BUILTIN_AVB_KEYS = {
    "builtin:rsa2048": "testkey_rsa2048.pem",
    "builtin:rsa4096": "testkey_rsa4096.pem",
}


def resolve_avb_key(value: str | None) -> str | None:
    if not value:
        return None
    if value in BUILTIN_AVB_KEYS:
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
        path = base / "assets" / "keys" / BUILTIN_AVB_KEYS[value]
    elif value.startswith("builtin:"):
        raise ValueError("未知的内置 AVB 测试密钥")
    else:
        path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"AVB 密钥不存在：{path}")
    return str(path)


def load_rsa_key(value: str, password: bytes | None = None):
    path = Path(resolve_avb_key(value))
    data = path.read_bytes()
    # OTA-generated encrypted keys share a passphrase file. Do not prompt in
    # a windowed worker, and never include key/password contents in errors.
    if password is None:
        pass_file = path.parent / "passphrase.txt"
        if pass_file.is_file():
            password = pass_file.read_bytes().rstrip(b"\r\n") or None
    try:
        if b"PRIVATE KEY-----" in data:
            key = serialization.load_pem_private_key(data, password=password if b"ENCRYPTED" in data else None)
        else:
            key = serialization.load_pem_public_key(data)
    except (ValueError, TypeError) as error:
        raise ValueError("无法读取 PEM 密钥，请检查密钥格式及 passphrase.txt 密码") from error
    if not isinstance(key, (rsa.RSAPrivateKey, rsa.RSAPublicKey)):
        raise ValueError("AVB 需要 RSA 密钥")
    public = key.public_key() if isinstance(key, rsa.RSAPrivateKey) else key
    if key.key_size not in (2048, 4096, 8192) or public.public_numbers().e != 65537:
        raise ValueError("AVB 需要 2048/4096/8192 位 RSA 密钥，公钥指数必须为 65537")
    return key


def avb_algorithm_for_key(value: str | None) -> str:
    return "NONE" if not value else f"SHA256_RSA{load_rsa_key(value).key_size}"
