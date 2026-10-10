"""Regression for the CLI menu screen clearing.

The menu called ``os.system("clear")``, which spawned a shell and printed
"'clear' is not recognized as an internal or external command" on Windows, even
though the module already imported the cross-platform ``clear_console`` helper.
"""
import contextlib
import io
import unittest
from pathlib import Path

from Scripts.Primary import Utils

ROOT = Path(__file__).resolve().parents[1]


class MenuConsoleTests(unittest.TestCase):
    def test_menu_uses_the_cross_platform_helper(self):
        source = (ROOT / "Scripts" / "Primary" / "Menu.py").read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines()
                         if not line.lstrip().startswith("#"))
        self.assertNotIn("os.system(", code)
        self.assertIn("clear_console()", code)

    def test_clear_console_is_safe_without_a_terminal(self):
        with contextlib.redirect_stdout(io.StringIO()):
            Utils.clear_console()


if __name__ == "__main__":
    unittest.main()
