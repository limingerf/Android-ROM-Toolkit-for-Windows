"""Native host-tool resolution; Linux is preserved, WSL is opt-in."""
from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import urllib.request
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


def _decode_tool_output(value: bytes) -> str:
    """Decode both native UTF-8 and wsl.exe's UTF-16 console output."""
    if b"\x00" in value:
        try:
            return value.decode("utf-16", "replace")
        except UnicodeError:
            pass
    return value.decode("utf-8", "replace")


def _print_tool_output(value: str) -> None:
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
# tool.  Tools with no dependable Windows port continue through WSL.
WINDOWS_TOOL_MANIFEST = {
    "img2simg": "https://raw.githubusercontent.com/Rprop/aosp15_partition_tools/main/windows_x86/img2simg.exe",
    "lpmake": "https://raw.githubusercontent.com/Rprop/aosp15_partition_tools/main/windows_x86/lpmake.exe",
    "simg2img": "https://raw.githubusercontent.com/Rprop/aosp15_partition_tools/main/windows_x86/simg2img.exe",
    "ext2simg": "https://raw.githubusercontent.com/Rprop/aosp15_partition_tools/main/windows_x86/ext2simg.exe",
    "lpunpack": "https://raw.githubusercontent.com/Rprop/aosp15_partition_tools/main/windows_x86/lpunpack.exe",
    "lpadd": "https://raw.githubusercontent.com/Rprop/aosp15_partition_tools/main/windows_x86/lpadd.exe",
    "lpdumps": "https://raw.githubusercontent.com/Rprop/aosp15_partition_tools/main/windows_x86/lpdumps.exe",
}


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
        if self.mode == "native" and self.wsl_bin_dir and self.wsl_executable:
            return "Windows 原生 + WSL 兼容工具"
        return {"native": "Windows 原生", "linux": "Linux 原生",
                "darwin": "macOS 外部工具", "wsl": "WSL（可选）"}.get(self.mode, self.mode)

    @property
    def available(self) -> bool:
        return self.mode != "wsl" or bool(self.wsl_executable)

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(name for name in REQUIRED_TOOLS if self.resolve(name, required=False) is None)

    def resolve(self, name: str, required: bool = True) -> str | None:
        if Path(name).is_absolute():
            if Path(name).is_file():
                return str(Path(name))
        else:
            suffixes = (".exe", ".py") if self.mode == "native" else ("", ".py")
            for directory in self.search_dirs or (self.bin_dir,):
                for suffix in suffixes:
                    candidate = directory / (name if Path(name).suffix in {".exe", ".py"} else name + suffix)
                    if candidate.is_file():
                        return str(candidate)
            if self.mode != "wsl":
                candidate = shutil.which(name)
                if candidate and (self.mode != "native" or candidate.lower().endswith((".exe", ".py"))):
                    return candidate
            # A Windows build can use the bundled Linux tools through WSL for
            # the small set that has no reliable Windows port (e2fsck, AVB,
            # and similar). The marker is handled by command() below.
            if self.mode == "native" and self.wsl_bin_dir and self.wsl_executable:
                fallback = self.wsl_bin_dir / name
                if fallback.is_file():
                    return f"wsl://{name}"
        if required:
            raise ToolchainError(
                f"缺少工具 {name}。将兼容工具放入 {self.bin_dir}，"
                "或在运行时页配置工具目录。其他功能仍可使用。")
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
        fallback = False
        if bundled:
            resolved = self.resolve(args[0])
            fallback = isinstance(resolved, str) and resolved.startswith("wsl://")
            args[0] = str(self.wsl_bin_dir / args[0]) if fallback else resolved
        if args[0].lower().endswith(".py") and self.mode != "wsl":
            args.insert(0, sys.executable)
        if self.mode == "wsl" or fallback:
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
        return subprocess.run(self.command(argv), env=self.environment(kwargs.pop("env", None)),
                              **process_options(), **kwargs)

    def run(self, argv, *, bundled=True, shell=False, output=True, env=None) -> int:
        if shell:
            if self.mode != "wsl":
                raise ToolchainError("平台工具调用必须使用参数列表；shell 插件请使用专用后端。")
            command = [self.wsl_executable or "wsl.exe", "--cd", windows_to_wsl(Path.cwd()), "--", "sh", "-lc", windows_to_wsl(str(argv))]
            child_env = self.environment(env)
            with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, env=child_env, **process_options()) as process:
                for line in iter(process.stdout.readline, b""):
                    if output:
                        _print_tool_output(_decode_tool_output(line))
                return process.wait()
        if isinstance(argv, str):
            if os.name == "nt":
                raise ToolchainError("Windows 工具调用必须使用参数列表，避免路径转义丢失。")
            argv = shlex.split(argv)
        with subprocess.Popen(self.command(argv, bundled), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, env=self.environment(env),
                              **process_options()) as process:
            for line in iter(process.stdout.readline, b""):
                if output:
                    _print_tool_output(_decode_tool_output(line))
            return process.wait()

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
                                      root / "art-res" / "bin") if p is not None and p.is_dir())
        wsl_bin = root / "art-res" / f"bin-{arch}"
        wsl_exe = shutil.which("wsl.exe")
        return Toolchain("native", root, bundled, directories, wsl_exe, wsl_bin if wsl_bin.is_dir() else None)
    bundled = root / "art-res" / f"bin-{arch}"
    directories = tuple(p for p in (root / "art-res" / "bin", bundled) if p.is_dir())
    mode = "linux" if sys.platform.startswith("linux") else "darwin"
    return Toolchain(mode, root, directories[0] if directories else bundled, directories)
