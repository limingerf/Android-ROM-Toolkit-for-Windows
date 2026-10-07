# -*- coding: utf-8 -*-
"""A.R.T command-line application entry point."""
import multiprocessing
import os
import sys
import argparse
from pathlib import Path


# Make the repository root importable when this file is executed directly.
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def _restore_windowed_stdio():
    """Use inherited pipes for worker/MCP without allocating a console.

    PyInstaller's windowed bootloader sets Python's standard streams to None,
    even when the parent passed valid Windows pipes. Duplicate those handles
    into CRT descriptors; plain desktop launches use NUL instead.
    """
    if os.name != 'nt' or not getattr(sys, 'frozen', False):
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.DuplicateHandle.argtypes = [
        wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
        wintypes.BOOL, wintypes.DWORD,
    ]
    kernel32.DuplicateHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    process = kernel32.GetCurrentProcess()
    for name, identifier, mode in (
        ('stdin', -10, 'r'), ('stdout', -11, 'w'), ('stderr', -12, 'w'),
    ):
        if getattr(sys, name) is not None:
            continue
        stream = None
        handle = kernel32.GetStdHandle(identifier & 0xffffffff)
        if handle not in (None, ctypes.c_void_p(-1).value):
            duplicate = wintypes.HANDLE()
            if kernel32.DuplicateHandle(process, handle, process,
                                        ctypes.byref(duplicate), 0, False, 2):
                flags = os.O_BINARY | (os.O_RDONLY if mode == 'r' else os.O_WRONLY)
                try:
                    fd = msvcrt.open_osfhandle(duplicate.value, flags)
                except OSError:
                    kernel32.CloseHandle(duplicate)
                else:
                    try:
                        stream = os.fdopen(fd, mode, encoding='utf-8', errors='replace', buffering=1)
                    except OSError:
                        os.close(fd)
        if stream is None:
            stream = open(os.devnull, mode, encoding='utf-8')
        setattr(sys, name, stream)
        setattr(sys, '__' + name + '__', stream)


def _configure_stdio_encoding():
    """Force UTF-8 text streams for runtimes that default to ASCII."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


_restore_windowed_stdio()
_configure_stdio_encoding()

def exception_handler(exception_type, exception, traceback):
    del traceback
    print("很抱歉，工具出现错误， 请把以下日志提交给开发者：")
    sys.stderr.write('{}: {}\n'.format(exception_type.__name__, exception))
    if input("是否重启 [1=重启/0=退出]") == "1":
        init()
    else:
        sys.exit(1)


def init():
    from Scripts.Primary.Menu import menu_once
    from Scripts.Primary.Settings import check_permissions
    from Scripts.Primary.Utils import init_bin_path
    init_bin_path()
    check_permissions()
    menu_once()


if __name__ == '__main__':
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument('--gui', action='store_true', help='启动桌面图形界面')
    parser.add_argument('--mcp', action='store_true', help='以 MCP stdio 服务运行')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--root', type=Path, help='工程根目录，默认应用目录')
    args, _unknown = parser.parse_known_args()
    # A Windows frozen build is the desktop application.  Keep the source
    # checkout's historical no-argument CLI behavior unchanged.
    if getattr(sys, "frozen", False) and not any((args.gui, args.mcp, args.worker)):
        args.gui = True
    if args.worker:
        from Scripts.worker import main as worker_main
        worker_main()
        raise SystemExit(0)
    if args.gui:
        from Scripts.gui import launch
        from Scripts.application import ROOT
        launch(args.root or ROOT)
        raise SystemExit(0)
    if args.mcp:
        from Scripts.mcp_server import main as mcp_main
        from Scripts.application import ROOT
        mcp_main(args.root or ROOT)
        raise SystemExit(0)
    sys.excepthook = exception_handler
    init()
