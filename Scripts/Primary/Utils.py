"""Shared utility functions grouped by responsibility.

Sections are intentionally kept in one stable common helper module for the
extract, remake, and primary packages.
"""

import os
import sys
import shlex
import shutil
import subprocess

from pathlib import Path

from Scripts.Platform.runtime import clear_screen, detect_toolchain


class LayoutError(RuntimeError):
    """Base error for invalid project layouts and unsafe paths."""


# ---------------------------------------------------------------------------
# Windows long-path support
# ---------------------------------------------------------------------------
# MAX_PATH - 1: the limit that applies while the LongPathsEnabled policy is off
# and no \\?\ prefix is used.  Measured on a real ColorOS system tree: the
# deepest entry sits 155 characters below WORKSPACE, so a project root that is
# already long leaves no room.
LONG_PATH_LIMIT = 259
TREE_MARGIN = 130


def long_paths_enabled():
    """Report whether Windows accepts paths longer than MAX_PATH."""
    if os.name != "nt":
        return True
    cached = getattr(long_paths_enabled, "_cached", None)
    if cached is not None:
        return cached
    enabled = False
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\FileSystem",
        ) as key:
            enabled = bool(winreg.QueryValueEx(key, "LongPathsEnabled")[0])
    except (OSError, ImportError, ValueError):
        enabled = False
    long_paths_enabled._cached = enabled
    return enabled


def extended_path(path):
    """Return a path Windows APIs accept beyond MAX_PATH.

    Only absolute paths can carry the prefix and it disables "." / ".."
    normalisation, so the value is normalised first.  Other platforms and
    already-prefixed values are returned unchanged.
    """
    text = os.fspath(path)
    if os.name != "nt" or text.startswith("\\\\?\\"):
        return text
    absolute = os.path.abspath(text)
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC" + absolute[1:]
    return "\\\\?\\" + absolute


def long_path_risk(root, margin=TREE_MARGIN):
    """Return an actionable warning when a project root risks MAX_PATH."""
    if long_paths_enabled():
        return None
    text = os.path.abspath(os.fspath(root))
    if len(text) + margin <= LONG_PATH_LIMIT:
        return None
    return (
        f"工程路径过长（{len(text)} 字符，加上目录树后可能超过 {LONG_PATH_LIMIT} 字符上限）："
        f"{text}\n"
        "  建议把工程移到更短的路径，或启用 Windows 长路径支持"
        "（HKLM\\SYSTEM\\CurrentControlSet\\Control\\FileSystem\\LongPathsEnabled 设为 1 后重启）。"
    )


# ---------------------------------------------------------------------------
# Runtime state and bundled external-tool paths
# ---------------------------------------------------------------------------
SOURCE_ROOT = Path(__file__).resolve().parents[2]
_RESOURCE_ROOT_CANDIDATES = [
    Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else None,
    SOURCE_ROOT,
    Path.cwd(),
]
ROOT_DIR = next(
    (candidate for candidate in _RESOURCE_ROOT_CANDIDATES
     if candidate is not None and (candidate / "art-res").is_dir()),
    SOURCE_ROOT,
)
PWD_DIR = str(ROOT_DIR) + os.sep


def _bundled_bin_path() -> Path:
    return detect_toolchain(ROOT_DIR).bin_dir


BIN_PATH = str(_bundled_bin_path()) + os.sep

RED, WHITE, CYAN, YELLOW, MAGENTA, GREEN, BOLD, CLOSE = [
    '\x1b[91m', '\x1b[97m', '\x1b[36m', '\x1b[93m',
    '\x1b[1;35m', '\x1b[1;32m', '\x1b[1m', '\x1b[0m',
]


# Global mutable state shared by the interactive workflow.
class GlobalValue(object):
    JM = False

    def __init__(self):
        self.programs = [
            "cpio", "brotli", "img2simg", "e2fsck", "resize2fs",
            "mke2fs", "e2fsdroid", "mkfs.erofs", "lpmake",
            "extract.erofs", "magiskboot", "avbroot",
        ]

    def __getattr__(self, item):
        return None


V = GlobalValue()
V.toolchain = detect_toolchain(ROOT_DIR, V.programs)
V.SETUP_MANIFEST = {}


# Runtime setup helpers.
def change_permissions_recursive(path, mode):
    if os.name == "nt":
        return
    for root, dirs, files in os.walk(path):
        for d in dirs:
            os.chmod(os.path.join(root, d), mode)
        for f in files:
            os.chmod(os.path.join(root, f), mode)
    os.chmod(path, mode)


def _normalize_bin_permissions(path):
    """Set bundled-tool directories to 0777 and regular files to 0755."""
    if os.name == "nt":
        return
    if os.path.islink(path):
        return
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(root, name))]
        for name in dirs:
            os.chmod(os.path.join(root, name), 0o777)
        for name in files:
            target = os.path.join(root, name)
            if not os.path.islink(target):
                os.chmod(target, 0o755)
    os.chmod(path, 0o777)


def init_bin_path():
    """Select a native Windows, WSL2, or Linux tool backend."""
    V.toolchain = detect_toolchain(ROOT_DIR, V.programs)
    if V.toolchain.mode == "unavailable":
        raise RuntimeError(
            "没有可用的镜像工具链。Windows 请安装 WSL2，或把 .exe 工具放入 "
            "art-res/bin-win-amd64。"
        )
    os.environ["PATH"] = str(V.toolchain.bin_dir) + os.pathsep + os.environ.get("PATH", "")
    _normalize_bin_permissions(str(V.toolchain.bin_dir))


# Execute one bundled tool while preserving the historical call API.
def call(exe, kz='Y', out=0, shstate=False, sp=0, env=None):
    """Run a command with MIO-compatible argv handling and no implicit shell."""
    del sp
    if isinstance(exe, (list, tuple)):
        cmd = [str(item) for item in exe if item not in (None, '')]
    elif shstate:
        cmd = exe
    else:
        cmd = shlex.split(str(exe))

    try:
        if kz == 'Y' and not shstate:
            return V.toolchain.run(cmd, bundled=True, output=(out == 0), env=env)
        process = subprocess.Popen(
            cmd,
            shell=shstate,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
    except (OSError, RuntimeError) as error:
        print(f'> 启动命令失败: {error}')
        return 127

    if process.stdout:
        for line in iter(process.stdout.readline, b''):
            if out == 0:
                print(line.decode('utf-8', 'ignore').strip())
    return process.wait()


def clear_console():
    """Compatibility alias for menu code and third-party integrations."""
    clear_screen()



# ---------------------------------------------------------------------------
# Ordinary filesystem helpers
# ---------------------------------------------------------------------------
def get_dir_size(ddir, max_=1.06):
    size = 0

    def _report(error):
        # os.walk() skips unreadable or unaddressable directories (a deep tree
        # below a long root, for instance) without a word, which under-reported
        # the size and could size the partition too small for its content.
        print(f"> 无法统计目录大小: {getattr(error, 'filename', ddir)} ({error})")

    for (root, dirs, files) in os.walk(ddir, onerror=_report):
        for name in files:
            file_path = os.path.join(root, name)
            if not os.path.islink(file_path):
                try:
                    size += os.path.getsize(file_path)
                except OSError:
                    continue
    return int(size * max_)


def ceil(x):
    if isinstance(x, int):
        return x
    if isinstance(x, float):
        int_part = int(x)
        if x > 0 and x > int_part:
            return int_part + 1
        return int_part
    return int(x)


def remove_tree(path, ignore_errors=False):
    """Remove a tree that may live beyond MAX_PATH.

    ``shutil.rmtree`` resolves the path itself, so a deep extraction tree below
    a long project root could not be removed on Windows at all.
    """
    shutil.rmtree(extended_path(path), ignore_errors=ignore_errors)


def rmdire(path):
    # A path beyond MAX_PATH is not visible to plain os.path.exists(), so the
    # old check silently did nothing for a deep extracted tree.
    target = extended_path(path)
    if os.path.exists(target):
        try:
            remove_tree(path)
        except PermissionError:
            print("无法删除文件夹，权限不足")
        else:
            print("删除成功！")


# Validate archive members before extracting into a project path.
def safe_extract_zip(archive, destination):
    """Extract a ZIP only after rejecting members that escape its destination."""
    import stat
    destination = Path(destination)
    if destination.is_symlink() or not destination.is_dir():
        raise LayoutError(f'ZIP 输出目录无效: {destination}')
    destination = destination.resolve()
    for member in archive.infolist():
        mode = (member.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode):
            raise LayoutError(f'ZIP 不支持链接或特殊文件: {member.filename}')
        target = (destination / member.filename).resolve()
        try:
            target.relative_to(destination)
        except ValueError as error:
            raise LayoutError(f'ZIP 包含越界路径: {member.filename}') from error
    archive.extractall(destination)


def findfile(file, dir_) -> str:
    """Walk dir_ and return the first path ending with file."""
    for root, dirs, files in os.walk(dir_, topdown=True):
        if file in files:
            return root + os.sep + file
