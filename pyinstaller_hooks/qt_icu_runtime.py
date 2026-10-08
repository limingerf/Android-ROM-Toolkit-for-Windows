"""Remove the incompatible ICU DLL collected by PyInstaller before imports."""
import sys
from pathlib import Path

if sys.platform.startswith("win"):
    _base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    for _name in ("icuuc.dll", "icudt78.dll", "icuin.dll"):
        for _candidate in (_base / _name, _base / "PySide6" / _name):
            try:
                _candidate.unlink(missing_ok=True)
            except OSError:
                pass
