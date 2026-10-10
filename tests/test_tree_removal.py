"""Regression for tree removal going through the long-path-aware helper.

``shutil.rmtree`` resolves the path itself, so a tree beyond MAX_PATH (a deep
extraction tree below a long project root) could not be removed on Windows.
Every module that can remove an extracted tree must use ``Utils.remove_tree``;
``Scripts/Extract/ext4.py`` keeps a local prefix helper because that module
deliberately imports no other A.R.T module.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "Scripts"

# Modules that may still call shutil.rmtree directly, with the reason.
ALLOWED = {
    # Its own body: the helper wraps shutil.rmtree.
    Path("Primary/Utils.py"),
    # Standalone by design: only stdlib imports, so it prefixes paths locally.
    Path("Extract/ext4.py"),
}


class TreeRemovalTests(unittest.TestCase):
    def _sources(self):
        for path in sorted(SCRIPTS.rglob("*.py")):
            if path.name == "avbtool.py":  # vendored upstream, out of scope
                continue
            yield path.relative_to(SCRIPTS), path

    def test_no_module_removes_trees_with_a_bare_path(self):
        offenders = []
        for relative, path in self._sources():
            if relative in ALLOWED:
                continue
            text = path.read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), 1):
                if "shutil.rmtree(" in line and not line.lstrip().startswith("#"):
                    offenders.append(f"{relative}:{number}")
        self.assertEqual(offenders, [], f"改用 Utils.remove_tree: {offenders}")

    def test_the_helper_is_actually_used(self):
        users = []
        for relative, path in self._sources():
            if relative in ALLOWED:
                continue
            if re.search(r"\bremove_tree\(", path.read_text(encoding="utf-8")):
                users.append(str(relative))
        self.assertGreaterEqual(len(users), 5, users)


if __name__ == "__main__":
    unittest.main()
