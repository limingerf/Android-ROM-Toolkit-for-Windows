"""Native host-tool resolution; Linux is preserved, WSL is opt-in."""
from __future__ import annotations

import json
import io
import hashlib
import os
import platform
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

REQUIRED_TOOLS = (
    "cpio", "brotli", "img2simg", "e2fsck", "resize2fs", "mke2fs",
    "e2fsdroid", "mkfs.erofs", "lpmake", "extract.erofs", "magiskboot",
    "avbroot", "avbtool",
)
CAPABILITIES = {
    "payload 提取": (), "EXT4 提取": (), "super 提取": (), "RAW / sparse 转换": (),
    "DAT.BR 解压": ("brotli",), "EROFS 提取": ("extract.erofs",),
    "boot 解包 / 回包": ("magiskboot",),
    "EXT4 回包": ("mke2fs", "e2fsdroid", "e2fsck"),
    "EROFS 回包": ("mkfs.erofs",), "super 合成": ("lpmake",),
    "OTA 签名": ("avbroot",), "AVB 操作": ("avbtool",),
}

class ToolchainError(RuntimeError):
    """A required operation-specific tool is missing or failed."""


def _output_encoding(value: bytes) -> str | None:
    if value.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16le" if value.startswith(b"\xff\xfe") else "utf-16be"
    # A stray NUL does not make a UTF-8 line UTF-16. Require an alternating
    # pattern near the start; wsl.exe diagnostics start with ASCII 'wsl:'.
    sample = value[:16]
    if len(sample) >= 4:
        even = sample[::2].count(0)
        odd = sample[1::2].count(0)
        if sample[1] == sample[3] == 0 and odd >= 2 and odd > even and odd >= len(sample) / 4:
            return "utf-16le"
        if sample[0] == sample[2] == 0 and even >= 2 and even > odd and even >= len(sample) / 4:
            return "utf-16be"
    return None


def _decode_tool_output(value: bytes) -> str:
    """Decode one complete line without guessing UTF-16 from a single NUL."""
    encoding = _output_encoding(value)
    if encoding:
        return value.decode(encoding, "replace").lstrip("\ufeff")
    value = value.lstrip(b"\x00")
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        if os.name == "nt":
            try:
                return value.decode("mbcs")
            except UnicodeDecodeError:
                pass
        return value.decode("utf-8", "replace")


class _ToolOutputDecoder:
    """Frame mixed UTF-16 launcher diagnostics and UTF-8 tool output.

    Byte readline() splits UTF-16LE LF before its final NUL. That leftover
    byte used to corrupt the following UTF-8 e2fsck banner. Split only at a
    complete, aligned newline and detect the encoding again for each line.
    """

    def __init__(self):
        self.pending = b""

    def feed(self, value: bytes, *, final=False) -> list[str]:
        self.pending += value
        lines = []
        while self.pending:
            if len(self.pending) < 4 and not final:
                break
            encoding = _output_encoding(self.pending)
            marker = {"utf-16le": b"\n\x00", "utf-16be": b"\x00\n"}.get(encoding, b"\n")
            offset = self.pending.find(marker)
            while encoding and offset >= 0 and offset % 2:
                offset = self.pending.find(marker, offset + 1)
            if offset < 0:
                if final:
                    lines.append(_decode_tool_output(self.pending))
                    self.pending = b""
                break
            end = offset + len(marker)
            lines.append(_decode_tool_output(self.pending[:end]))
            self.pending = self.pending[end:]
        return lines


def _stream_tool_output(process, output: bool) -> int:
    """Drain both pipes concurrently and print decoded lines on one thread."""
    events = queue.Queue()

    def read(stream):
        decoder = _ToolOutputDecoder()
        try:
            while chunk := stream.read1(65536):
                for line in decoder.feed(chunk):
                    events.put(line)
            for line in decoder.feed(b"", final=True):
                events.put(line)
        except Exception as error:
            events.put(error)
        finally:
            events.put(None)

    readers = [threading.Thread(target=read, args=(stream,), daemon=True)
               for stream in (process.stdout, process.stderr)]
    for reader in readers:
        reader.start()
    remaining = len(readers)
    failures = []
    while remaining:
        event = events.get()
        if event is None:
            remaining -= 1
        elif isinstance(event, Exception):
            failures.append(event)
        elif output:
            _print_tool_output(event)
    for reader in readers:
        reader.join()
    code = process.wait()
    if failures:
        raise ToolchainError(f"读取工具输出失败：{failures[0]}")
    return code


def _print_tool_output(value: str) -> None:
    # Apply terminal backspaces before displaying mke2fs progress in Qt.
    cleaned = []
    for character in value.replace("\r\n", "\n"):
        if character == "\b":
            if cleaned:
                cleaned.pop()
        elif character == "\r":
            cleaned.clear()
        elif character != "\x00":
            cleaned.append(character)
    value = "".join(cleaned)
    try:
        print(value.rstrip(), flush=True)
    except UnicodeEncodeError:
        stream = getattr(sys, "stdout", None)
        if stream is not None:
            stream.buffer.write((value.rstrip() + "\n").encode(stream.encoding or "utf-8", "replace"))
            stream.flush()


# Small, statically linked Windows builds maintained by the Android partition
# tools project.  The app downloads only files that are still missing and uses
# an atomic rename so an interrupted download never becomes a usable-looking
# tool. WSL is used only when explicitly selected as the backend.
WINDOWS_TOOL_MANIFEST = {
    "img2simg": "https://raw.githubusercontent.com/Rprop/aosp15_partition_tools/main/windows_x86/img2simg.exe",
    "lpmake": "https://raw.githubusercontent.com/Rprop/aosp15_partition_tools/main/windows_x86/lpmake.exe",
    "simg2img": "https://raw.githubusercontent.com/Rprop/aosp15_partition_tools/main/windows_x86/simg2img.exe",
    "ext2simg": "https://raw.githubusercontent.com/Rprop/aosp15_partition_tools/main/windows_x86/ext2simg.exe",
    "lpunpack": "https://raw.githubusercontent.com/Rprop/aosp15_partition_tools/main/windows_x86/lpunpack.exe",
    "lpadd": "https://raw.githubusercontent.com/Rprop/aosp15_partition_tools/main/windows_x86/lpadd.exe",
    "lpdumps": "https://raw.githubusercontent.com/Rprop/aosp15_partition_tools/main/windows_x86/lpdumps.exe",
}


BUILTIN_AVBTOOL = "builtin://avbtool"
_AVBROOT_RELEASE_API = "https://api.github.com/repos/chenxiaolong/avbroot/releases/latest"


def _native_avbroot_version(executable: Path) -> str:
    """Validate a real Windows executable, rather than reporting file presence."""
    result = subprocess.run([str(executable), "--version"], capture_output=True,
                            timeout=15, **process_options())
    output = _decode_tool_output(result.stdout).strip()
    if result.returncode or not re.fullmatch(r"avbroot \d+\.\d+\.\d+(?:[-+][\w.]+)?", output):
        raise ToolchainError(f"原生 avbroot 无法运行：{output or _decode_tool_output(result.stderr).strip()}")
    return output.split(" ", 1)[1]


def bootstrap_native_avbroot(root: str | os.PathLike[str], *, progress=None) -> dict:
    """Install official Windows avbroot after SHA256 and executable checks.

    GitHub publishes the asset digest in its release API. Refuse missing or
    mismatched digests, and validate the staged executable before replacing
    an existing file. The one-file Windows build needs no external DLLs.
    """
    if os.name != "nt" or _architecture() != "amd64":
        return {"downloaded": []}
    target = Path(root).resolve() / "art-res" / "bin-win-amd64"
    executable = target / "avbroot.exe"
    if executable.is_file():
        try:
            return {"downloaded": [], "directory": str(target),
                    "version": _native_avbroot_version(executable)}
        except (OSError, subprocess.SubprocessError, ToolchainError):
            pass
    target.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "Android-ROM-Toolkit", "Accept": "application/vnd.github+json"}
    if progress:
        progress("查询官方 Windows avbroot 最新版本")
    try:
        with urllib.request.urlopen(urllib.request.Request(_AVBROOT_RELEASE_API, headers=headers), timeout=60) as response:
            metadata = response.read(1024 * 1024 + 1)
        if len(metadata) > 1024 * 1024:
            raise ToolchainError("avbroot 官方版本信息过大")
        release = json.loads(metadata)
        version = str(release.get("tag_name", "")).removeprefix("v")
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][\w.]+)?", version):
            raise ToolchainError("avbroot 官方版本信息无效")
        filename = f"avbroot-{version}-x86_64-pc-windows-msvc.zip"
        asset = next((item for item in release.get("assets", []) if item.get("name") == filename), None)
        if not asset:
            raise ToolchainError("官方最新 avbroot 没有 Windows x64 发布包")
        digest = str(asset.get("digest", ""))
        if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
            raise ToolchainError("官方 avbroot 发布包没有 SHA256 校验信息，已停止下载")
        url = str(asset.get("browser_download_url", ""))
        expected_url = f"https://github.com/chenxiaolong/avbroot/releases/download/{release['tag_name']}/{filename}"
        if url != expected_url:
            raise ToolchainError("avbroot 官方下载地址无效")
        if progress:
            progress(f"下载原生 avbroot {version}")
        with tempfile.TemporaryDirectory(prefix=".avbroot-download-", dir=target) as temporary:
            stage = Path(temporary)
            archive = stage / filename
            checksum = hashlib.sha256()
            total = 0
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response, archive.open("wb") as destination:
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > 64 * 1024 * 1024:
                        raise ToolchainError("avbroot 发布包超过允许大小")
                    checksum.update(chunk)
                    destination.write(chunk)
            if checksum.hexdigest() != digest.split(":", 1)[1].lower():
                raise ToolchainError(f"原生 avbroot SHA256 校验失败：{filename}")
            with zipfile.ZipFile(archive) as package:
                for name in ("avbroot.exe", "LICENSE", "README.md"):
                    entry = package.getinfo(name)
                    file_type = (entry.external_attr >> 16) & 0o170000
                    if entry.is_dir() or file_type not in (0, 0o100000) or entry.file_size > 64 * 1024 * 1024:
                        raise ToolchainError(f"avbroot 发布包文件无效：{name}")
                    with package.open(entry) as source, (stage / name).open("wb") as destination:
                        shutil.copyfileobj(source, destination)
            installed_version = _native_avbroot_version(stage / "avbroot.exe")
            if installed_version != version:
                raise ToolchainError("avbroot 可执行文件版本与官方发布包不一致")
            license_directory = target / "licenses" / "avbroot"
            license_directory.mkdir(parents=True, exist_ok=True)
            for name in ("LICENSE", "README.md"):
                os.replace(stage / name, license_directory / name)
            os.replace(stage / "avbroot.exe", executable)
        return {"downloaded": ["avbroot.exe", "licenses/avbroot/LICENSE", "licenses/avbroot/README.md"],
                "directory": str(target), "version": installed_version}
    except ToolchainError:
        raise
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        raise ToolchainError(f"补齐原生 avbroot 失败：{error}") from error


_CYGWIN_MIRROR = "https://mirrors.kernel.org/sourceware/cygwin/x86_64/release/"
# The maintained Cygwin Windows port of e2fsprogs is 1.44.5. Keep it isolated
# from the DLLs used by mke2fs/e2fsdroid, and never substitute a Linux binary.
_WINDOWS_EXT4_PACKAGES = (
    ("e2fsprogs/e2fsprogs-1.44.5-1.tar.xz", ("usr/sbin/e2fsck.exe", "usr/sbin/resize2fs.exe")),
    ("e2fsprogs/libcom_err2/libcom_err2-1.44.5-1.tar.xz", ("usr/bin/cygcom_err-2.dll",)),
    ("e2fsprogs/libe2p2/libe2p2-1.44.5-1.tar.xz", ("usr/bin/cyge2p-2.dll",)),
    ("e2fsprogs/libext2fs2/libext2fs2-1.44.5-1.tar.xz", ("usr/bin/cygext2fs-2.dll",)),
    ("util-linux/libblkid1/libblkid1-2.40.2-2.tar.xz", ("usr/bin/cygblkid-1.dll",)),
    ("util-linux/libuuid1/libuuid1-2.40.2-2.tar.xz", ("usr/bin/cyguuid-1.dll",)),
    ("gettext/libintl8/libintl8-0.22.5-1.tar.xz", ("usr/bin/cygintl-8.dll",)),
    ("libiconv/libiconv2/libiconv2-1.17-1.tar.xz", ("usr/bin/cygiconv-2.dll",)),
    ("gcc/libgcc1/libgcc1-14.4.0-1-x86_64.tar.zst", ("usr/bin/cyggcc_s-seh-1.dll",)),
    ("cygwin/cygwin-3.6.11-1-x86_64.tar.xz", ("usr/bin/cygwin1.dll",)),
)


def bootstrap_native_ext4_tools(root: str | os.PathLike[str], *, progress=None) -> dict:
    """Install Windows EXT4 check/resize tools and their complete DLL set."""
    if os.name != "nt" or _architecture() != "amd64":
        return {"downloaded": []}
    parent = Path(root).resolve() / "art-res" / "bin-win-amd64"
    target = parent / "e2fsprogs"
    files = [Path(member).name for _, members in _WINDOWS_EXT4_PACKAGES for member in members]
    if all((target / name).is_file() and (target / name).stat().st_size > 1024 for name in files):
        return {"downloaded": [], "directory": str(target)}
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".ext4-download-", dir=parent) as temporary:
        stage = Path(temporary)
        for package, members in _WINDOWS_EXT4_PACKAGES:
            if progress:
                progress(f"下载原生 EXT4 依赖：{Path(package).name}")
            url = _CYGWIN_MIRROR + package
            headers = {"User-Agent": "Android-ROM-Toolkit"}
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
                archive = response.read()
            with urllib.request.urlopen(urllib.request.Request(url.rsplit("/", 1)[0] + "/sha512.sum", headers=headers), timeout=60) as response:
                checksums = response.read().decode("ascii")
            filename = Path(package).name
            expected = next((line.split()[0] for line in checksums.splitlines()
                             if len(line.split()) == 2 and line.split()[1].lstrip("*") == filename), None)
            if not expected or hashlib.sha512(archive).hexdigest() != expected.lower():
                raise ToolchainError(f"原生工具包 SHA512 校验失败：{filename}")
            if package.endswith(".zst"):
                import zstandard
                with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(archive)) as reader:
                    archive = reader.read()
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as package_tar:
                for member in members:
                    entry = package_tar.getmember(member)
                    if not entry.isfile():
                        raise ToolchainError(f"原生工具包中不是普通文件：{member}")
                    with package_tar.extractfile(entry) as source, (stage / Path(member).name).open("wb") as destination:
                        shutil.copyfileobj(source, destination)
            if any((stage / Path(member).name).stat().st_size <= 1024 for member in members):
                raise ToolchainError(f"原生工具包文件不完整：{filename}")
        # All archives and dependencies are checked before replacing files.
        target.mkdir(parents=True, exist_ok=True)
        for name in files:
            os.replace(stage / name, target / name)
    return {"downloaded": [f"e2fsprogs/{name}" for name in files], "directory": str(target)}


def bootstrap_windows_tools(root: str | os.PathLike[str], *, progress=None) -> dict:
    """Download missing, redistributable Windows helpers into art-res.

    This deliberately keeps the list small and explicit.  The core A.R.T
    binaries are shipped in the release; this function fills optional Android
    partition helpers when a source checkout was copied without them.
    """
    if os.name != "nt":
        return {"downloaded": [], "missing": []}
    target = Path(root).resolve() / "art-res" / "bin-win-amd64"
    target.mkdir(parents=True, exist_ok=True)
    downloaded = []
    errors = []
    for name, url in WINDOWS_TOOL_MANIFEST.items():
        destination = target / f"{name}.exe"
        if destination.is_file() and destination.stat().st_size > 1024:
            continue
        temporary = destination.with_suffix(destination.suffix + ".download")
        try:
            if progress:
                progress(f"下载 {destination.name}")
            request = urllib.request.Request(url, headers={"User-Agent": "A.R.T-toolkit"})
            with urllib.request.urlopen(request, timeout=60) as response, open(temporary, "wb") as stream:
                shutil.copyfileobj(response, stream)
            if temporary.stat().st_size <= 1024:
                raise RuntimeError("下载内容太小")
            os.replace(temporary, destination)
            downloaded.append(destination.name)
        except Exception as error:  # network state should not prevent other features
            errors.append(f"{destination.name}: {error}")
            try:
                temporary.unlink()
            except OSError:
                pass
    try:
        downloaded.extend(bootstrap_native_ext4_tools(root, progress=progress)["downloaded"])
    except Exception as error:
        errors.append(f"原生 EXT4 校验工具：{error}")
    try:
        downloaded.extend(bootstrap_native_avbroot(root, progress=progress)["downloaded"])
    except Exception as error:
        errors.append(f"原生 OTA 签名工具：{error}")
    return {"downloaded": downloaded, "errors": errors, "directory": str(target)}

def _architecture() -> str:
    return "arm64" if platform.machine().lower() in {"aarch64", "arm64"} else "amd64"

def windows_to_wsl(value: str | os.PathLike[str]) -> str:
    raw = os.fspath(value)
    # Plain paths and --file-contexts=C:\... style options.
    return re.sub(r"([A-Za-z]):[/\\]([^\r\n]*)",
                  lambda m: "/mnt/" + m[1].lower() + "/" + m[2].replace("\\", "/"), raw)

def clear_screen() -> None:
    if sys.stdout is not None and sys.stdout.isatty():
        print("\033[2J\033[H", end="", flush=True)

def process_options() -> dict:
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

@dataclass(frozen=True)
class Toolchain:
    mode: str
    root: Path
    bin_dir: Path
    search_dirs: tuple[Path, ...] = ()
    wsl_executable: str | None = None
    wsl_bin_dir: Path | None = None

    @property
    def label(self) -> str:
        return {"native": "Windows 原生", "linux": "Linux 原生",
                "darwin": "macOS 外部工具", "wsl": "WSL（可选）"}.get(self.mode, self.mode)

    @property
    def available(self) -> bool:
        return self.mode != "wsl" or bool(self.wsl_executable)

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(name for name in REQUIRED_TOOLS if self.resolve(name, required=False) is None)

    def resolve(self, name: str, required: bool = True) -> str | None:
        if name == BUILTIN_AVBTOOL and self.mode == "native":
            return BUILTIN_AVBTOOL
        is_avbtool = Path(name).name.lower() in {"avbtool", "avbtool.py", "avbtool.exe"}
        if Path(name).is_absolute():
            if Path(name).is_file():
                if self.mode == "native" and is_avbtool and name.lower().endswith(".py") and getattr(sys, "frozen", False):
                    return BUILTIN_AVBTOOL
                return str(Path(name))
        else:
            suffixes = (".exe", ".py") if self.mode == "native" else ("", ".py")
            for directory in self.search_dirs or (self.bin_dir,):
                for suffix in suffixes:
                    candidate = directory / (name if Path(name).suffix in {".exe", ".py"} else name + suffix)
                    if candidate.is_file():
                        if self.mode == "native" and is_avbtool and candidate.suffix.lower() == ".py" and getattr(sys, "frozen", False):
                            return BUILTIN_AVBTOOL
                        return str(candidate)
            if self.mode != "wsl":
                candidate = shutil.which(name)
                if candidate and (self.mode != "native" or candidate.lower().endswith((".exe", ".py"))):
                    if self.mode == "native" and is_avbtool and candidate.lower().endswith(".py") and getattr(sys, "frozen", False):
                        return BUILTIN_AVBTOOL
                    return candidate
        if self.mode == "native" and is_avbtool:
            return BUILTIN_AVBTOOL
        if self.mode == "native" and required and Path(name).name.lower() in {"avbroot", "avbroot.exe"}:
            result = bootstrap_native_avbroot(self.root, progress=_print_tool_output)
            candidate = Path(result.get("directory", self.bin_dir)) / "avbroot.exe"
            if candidate.is_file():
                return str(candidate)
        if required:
            raise ToolchainError(
                f"缺少工具 {name}。将兼容工具放入 {self.bin_dir}，"
                "或在工具链页补齐原生工具 / 配置工具目录。"
                "WSL 需要在工具链页明确选择并保存。")
        return None

    def diagnostics(self) -> dict:
        tools = {name: self.resolve(name, required=False) for name in REQUIRED_TOOLS}
        capabilities = {label: all(tools.get(tool) for tool in required)
                        for label, required in CAPABILITIES.items()}
        return {"mode": self.mode, "label": self.label, "root": str(self.root),
                "bin_dir": str(self.bin_dir), "ready": self.available,
                "missing": [name for name, path in tools.items() if not path],
                "tools": tools, "capabilities": capabilities}

    def ensure(self) -> None:
        if not self.available:
            raise ToolchainError("已选择 WSL 后端，但找不到 wsl.exe。")

    def command(self, argv: Sequence[str | os.PathLike[str]], bundled: bool = True) -> list[str]:
        self.ensure()
        args = [os.fspath(item) for item in argv]
        if not args:
            raise ValueError("命令不能为空")
        if bundled:
            args[0] = self.resolve(args[0])
        if args[0] == BUILTIN_AVBTOOL:
            if self.mode != "native":
                raise ToolchainError("内置 avbtool 入口仅用于 Windows 原生后端")
            if getattr(sys, "frozen", False):
                return [sys.executable, "--avbtool", *args[1:]]
            entry = Path(__file__).resolve().parents[1] / "main.py"
            return [sys.executable, str(entry), "--avbtool", *args[1:]]
        if self.mode == "native" and getattr(sys, "frozen", False) and Path(args[0]).name.lower() == "avbtool.py":
            return [sys.executable, "--avbtool", *args[1:]]
        if args[0].lower().endswith(".py") and self.mode != "wsl":
            args.insert(0, sys.executable)
        if self.mode == "wsl":
            args = [windows_to_wsl(value) for value in args]
            if bundled and self.mode == "wsl":
                # The local ZIP may not carry executable bits on NTFS.
                script = 'chmod +x "$1" && exec "$@"'
                return [self.wsl_executable or "wsl.exe", "--cd", windows_to_wsl(Path.cwd()),
                        "--", "sh", "-c", script, "art", *args]
            return [self.wsl_executable or "wsl.exe", "--cd", windows_to_wsl(Path.cwd()), "--", *args]
        return args

    def environment(self, env: Mapping[str, str] | None = None) -> dict:
        child = os.environ.copy()
        if env:
            child.update({str(key): str(value) for key, value in env.items()})
        if self.mode != "wsl":
            child["PATH"] = os.pathsep.join(str(p) for p in self.search_dirs or (self.bin_dir,)) + os.pathsep + child.get("PATH", "")
        return child

    def capture(self, argv, **kwargs):
        """subprocess.run-compatible adapter for legacy AVB/OTA functions."""
        text_output = kwargs.pop("text", False)
        text_output = kwargs.pop("universal_newlines", False) or text_output
        encoding = kwargs.pop("encoding", None)
        errors = kwargs.pop("errors", None)
        if isinstance(kwargs.get("input"), str):
            kwargs["input"] = kwargs["input"].encode(encoding or "utf-8", errors or "strict")
        result = subprocess.run(self.command(argv), env=self.environment(kwargs.pop("env", None)),
                                **process_options(), **kwargs)
        if text_output or encoding or errors:
            for name in ("stdout", "stderr"):
                value = getattr(result, name)
                if value is not None:
                    decoder = _ToolOutputDecoder()
                    setattr(result, name, "".join(decoder.feed(value, final=True)))
        return result

    def run(self, argv, *, bundled=True, shell=False, output=True, env=None) -> int:
        if shell:
            if self.mode != "wsl":
                raise ToolchainError("平台工具调用必须使用参数列表；shell 插件请使用专用后端。")
            command = [self.wsl_executable or "wsl.exe", "--cd", windows_to_wsl(Path.cwd()), "--", "sh", "-lc", windows_to_wsl(str(argv))]
            child_env = self.environment(env)
            with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=child_env, **process_options()) as process:
                return _stream_tool_output(process, output)
        if isinstance(argv, str):
            if os.name == "nt":
                raise ToolchainError("Windows 工具调用必须使用参数列表，避免路径转义丢失。")
            argv = shlex.split(argv)
        command = self.command(argv, bundled)
        if output:
            _print_tool_output(f"工具后端：{self.label} · {Path(command[0]).name}")
        with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=self.environment(env),
                              **process_options()) as process:
            return _stream_tool_output(process, output)

def detect_toolchain(root: str | os.PathLike[str], required=REQUIRED_TOOLS) -> Toolchain:
    del required  # Check tools per operation, not at application startup.
    root = Path(root).resolve()
    config_path = root / "art-res" / "host-tools.local.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    arch = _architecture()
    backend = os.environ.get("ART_BACKEND") or config.get("backend", "native")
    if os.name == "nt" and backend == "wsl":
        bundled = root / "art-res" / f"bin-{arch}"
        if not bundled.is_dir():
            bundled = root / "art-res" / "bin"
        return Toolchain("wsl", root, bundled, (bundled,), shutil.which("wsl.exe"), bundled)
    if os.name == "nt":
        bundled = root / "art-res" / f"bin-win-{arch}"
        configured = os.environ.get("ART_WINDOWS_TOOLS") or config.get("windows_tools", "")
        directories = tuple(p for p in (Path(configured).expanduser() if configured else None, bundled,
                                      bundled / "e2fsprogs",
                                      root / "art-res" / "bin") if p is not None and p.is_dir())
        return Toolchain("native", root, bundled, directories)
    bundled = root / "art-res" / f"bin-{arch}"
    directories = tuple(p for p in (root / "art-res" / "bin", bundled) if p.is_dir())
    mode = "linux" if sys.platform.startswith("linux") else "darwin"
    return Toolchain(mode, root, directories[0] if directories else bundled, directories)
