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


# ---------------------------------------------------------------------------
# Case-insensitive filesystem collisions
# ---------------------------------------------------------------------------
# Android filesystems are case sensitive, so one directory can hold both
# "ES-Vodafone" and "es-vodafone". NTFS supports this when the parent directory
# has its case-sensitive flag enabled; unmarked directories can merge the names.
# Verify the actual parent flag before refusing an operation.
CASE_COLLISION_REMEDY = (
    "请开启 Windows 开发者模式（或以管理员身份运行应用）后重新解包，"
    "让程序在创建分区目录前启用大小写敏感；也可改用 WSL 后端。"
    "不要在已包含内容的目录上直接运行 fsutil，Windows 不允许事后更改该标记。"
)


def _case_sensitive_info(path, *, set_enabled=False):
    """Query or set the NTFS per-directory case-sensitive flag."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        class FileCaseSensitiveInfo(ctypes.Structure):
            _fields_ = [("Flags", wintypes.ULONG)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        )
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.GetFileInformationByHandleEx.argtypes = (
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        )
        kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        kernel32.SetFileInformationByHandle.argtypes = (
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        )
        kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        path = os.path.abspath(os.fspath(path))
        access = 0x80 | (0x100 if set_enabled else 0)  # FILE_READ/WRITE_ATTRIBUTES
        handle = kernel32.CreateFileW(
            path, access, 0x1 | 0x2 | 0x4, None, 3, 0x02000000, None,
        )
        if handle == ctypes.c_void_p(-1).value:
            return False
        try:
            info = FileCaseSensitiveInfo()
            if not kernel32.GetFileInformationByHandleEx(
                handle, 23, ctypes.byref(info), ctypes.sizeof(info),
            ):
                return False
            if info.Flags & 1:
                return True
            if not set_enabled:
                return False
            info.Flags |= 1
            return bool(kernel32.SetFileInformationByHandle(
                handle, 23, ctypes.byref(info), ctypes.sizeof(info),
            ))
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def is_case_sensitive_directory(path):
    """Return whether a directory distinguishes case-variant child names."""
    return _case_sensitive_info(path)


def enable_case_sensitive_directory(path):
    """Enable case-sensitive child names on an existing directory."""
    path = Path(path)
    if path.is_symlink() or is_directory_junction(path) or not path.is_dir():
        return False
    return _case_sensitive_info(path, set_enabled=True)


def _raise_unrepresentable_case_collisions(root, collisions):
    collisions = [
        item for item in collisions
        if not is_case_sensitive_directory(item[0])
    ]
    if not collisions:
        return
    detail = describe_case_collisions(collisions, root)
    raise LayoutError(
        f"目录中存在未启用大小写敏感的冲突名称（{detail}）。"
        "Windows 无法在该目录中分别访问这些名称，继续操作可能合并内容。"
        + CASE_COLLISION_REMEDY
    )


def _colliding_names(names):
    """Return the name groups in *names* that differ only in case."""
    seen = {}
    for name in names:
        seen.setdefault(name.lower(), []).append(name)
    return [sorted(group) for group in seen.values() if len(group) > 1]


def find_case_collisions(root, limit=None):
    """Return names under *root* that differ only in case.

    Each item is ``(directory, kind, names)`` where ``kind`` is ``'dir'`` or
    ``'file'`` and ``names`` lists every colliding spelling.  Directory
    symlinks are not followed, so a linked tree is never scanned twice.

    A collision is only visible when the filesystem kept both spellings.  NTFS
    does keep them for directories created through its POSIX namespace, which
    is how an extraction produced the colliding workspace in the first place.
    """
    collisions = []
    visited = set()
    for current, dirs, files in os.walk(root, followlinks=False):
        try:
            identity = os.path.realpath(current)
        except OSError:
            identity = current
        if identity in visited:
            dirs[:] = []
            continue
        visited.add(identity)
        for names, kind in ((dirs, 'dir'), (files, 'file')):
            for group in _colliding_names(names):
                collisions.append((current, kind, group))
                if limit is not None and len(collisions) >= limit:
                    return collisions
    return collisions


def describe_case_collisions(collisions, root=None, limit=5):
    """Format collisions for a user-facing error message."""
    lines = []
    for directory, kind, names in collisions[:limit]:
        location = directory
        if root is not None:
            try:
                relative = os.path.relpath(directory, root)
            except ValueError:
                relative = directory
            if relative != os.curdir:
                location = f"{root}{os.sep}{relative}"
        lines.append(f"{location} ({kind}: {' / '.join(names)})")
    if len(collisions) > limit:
        lines.append(f"另有 {len(collisions) - limit} 处")
    return "；".join(lines)


def copy_tree(source, destination):
    """Copy a directory tree, preserving symbolic links.

    ``shutil.copytree`` cannot be used here: it calls ``os.makedirs`` without
    ``exist_ok`` for every directory it meets, so on Windows a partition that
    contains two names differing only in case can abort with ``WinError 183``.
    For a case-sensitive source tree, this walk marks the fresh destination
    root before creating children, then copies names and metadata exactly.

    Directory junctions are skipped: they are a Windows-only mount point that
    ``os.walk`` follows even with ``followlinks=False``, so descending into one
    could copy a tree twice or never finish.
    """
    source = os.fspath(source)
    destination = os.fspath(destination)
    collisions = find_case_collisions(source) if os.name == "nt" else []
    _raise_unrepresentable_case_collisions(source, collisions)
    os.makedirs(extended_path(destination), exist_ok=True)
    if collisions and not enable_case_sensitive_directory(destination):
        raise LayoutError(
            f"无法为回包暂存目录启用大小写敏感: {destination}。"
            "请开启 Windows 开发者模式或以管理员身份运行应用后重试回包，或改用 WSL。"
        )
    failures = []
    for root, dirs, files in os.walk(source, followlinks=False):
        relative = os.path.relpath(root, source)
        target_root = destination if relative == os.curdir else os.path.join(destination, relative)
        for name in list(dirs):
            entry = os.path.join(root, name)
            if is_directory_junction(entry):
                dirs.remove(name)
                continue
            if os.path.islink(entry):
                dirs.remove(name)
                failures.extend(_copy_entry(entry, os.path.join(target_root, name)))
                continue
            os.makedirs(extended_path(os.path.join(target_root, name)), exist_ok=True)
        for name in files:
            entry = os.path.join(root, name)
            failures.extend(_copy_entry(entry, os.path.join(target_root, name)))
    if failures:
        raise shutil.Error(failures)
    return destination


def is_directory_junction(path):
    """Report whether *path* is a Windows directory junction (mount point)."""
    if os.name != 'nt':
        return False
    try:
        import stat as stat_module
        info = os.lstat(extended_path(path))
    except OSError:
        return False
    return getattr(info, 'st_reparse_tag', 0) == stat_module.IO_REPARSE_TAG_MOUNT_POINT


def _copy_entry(source, destination):
    """Copy one file or link, returning ``shutil.Error`` style failures."""
    if is_directory_junction(source):
        return []
    try:
        if os.path.islink(source):
            target = os.readlink(source)
            try:
                os.symlink(target, destination, target_is_directory=os.path.isdir(source))
            except FileExistsError:
                return []
            except (OSError, NotImplementedError):
                # Without the symbolic-link privilege the link cannot be
                # recreated; a regular copy keeps the payload usable and the
                # caller's symlink audit reports what was lost.
                if os.path.isdir(source):
                    os.makedirs(extended_path(destination), exist_ok=True)
                else:
                    shutil.copy2(extended_path(source), extended_path(destination))
        else:
            shutil.copy2(extended_path(source), extended_path(destination))
    except OSError as error:
        return [(source, destination, str(error))]
    return []


def raise_on_case_collisions(root):
    """Refuse to continue when *root* cannot be represented on this filesystem.

    On Windows, collisions are safe only when their containing directories have
    the NTFS case-sensitive flag enabled. POSIX hosts preserve these names by
    default and do not need the check.
    """
    if os.name != 'nt':
        return
    collisions = find_case_collisions(root)
    _raise_unrepresentable_case_collisions(root, collisions)
