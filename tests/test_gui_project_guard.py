"""GUI slots must validate the project before they use it.

``_require_project()`` returns ``None`` -- after telling the user -- when no
project is selected.  Using that value first raises ``TypeError`` inside a Qt
slot (``Path / None``), which the window already had to fix once for
``Path / False`` in ``_open_project``.  This keeps the pattern from coming back.
"""
import ast
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "Scripts" / "qt_gui.py"


def is_require_project_call(node):
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_require_project")


def is_assignment(statement):
    if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
        return False
    value = statement.value
    if is_require_project_call(value):
        return True
    # project = self._require_project(); rows = ... keeps both on one line.
    return any(is_require_project_call(item) for item in ast.walk(value))


def mentions_project(statement):
    return any(isinstance(node, ast.Name) and node.id == "project"
               and isinstance(node.ctx, ast.Load)
               for node in ast.walk(statement))


def is_guard(statement):
    return isinstance(statement, ast.If) and mentions_project(statement)


class ProjectGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(SOURCE.read_text(encoding="utf-8"))

    def test_the_scan_finds_the_require_project_call_sites(self):
        assignments = [node for node in ast.walk(self.tree) if is_assignment(node)]
        self.assertGreaterEqual(len(assignments), 10,
                                "未找到 _require_project() 赋值，扫描逻辑可能失效")

    def test_project_is_never_used_before_its_guard(self):
        offenders = []
        for function in [node for node in ast.walk(self.tree)
                         if isinstance(node, ast.FunctionDef)]:
            pending = False
            for statement in function.body:
                if pending and mentions_project(statement) and not is_guard(statement):
                    offenders.append(f"{function.name}():{statement.lineno}")
                if is_assignment(statement):
                    pending = True
                elif is_guard(statement):
                    pending = False
        self.assertEqual(offenders, [],
                         "以下位置在 _require_project() 的 None 检查之前使用了 project")


if __name__ == "__main__":
    unittest.main()
