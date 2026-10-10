"""payload.py prompts must never block the GUI/worker path.

The module is an interactive CLI, but it shares ``V`` with the GUI and worker
entry points, where ``V.JM`` is set and there is no console to answer on.  A bare
``input()`` there hangs the job (or the GUI thread) forever, so every prompt goes
through ``_prompt`` and returns its skip/cancel answer instead.
"""
import ast
import unittest
from pathlib import Path
from unittest import mock

from Scripts.Primary.Utils import V
from Scripts.ReMake import payload

SOURCE = Path(__file__).resolve().parents[1] / "Scripts" / "ReMake" / "payload.py"


class DeadStdin:
    """Stands in for stdin so a missing guard fails fast instead of hanging."""

    def __init__(self, error):
        self.error = error

    def readline(self):
        raise self.error

    def isatty(self):
        return False


def calls_named(tree, name):
    return [node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == name]


class PayloadPromptTests(unittest.TestCase):
    def setUp(self):
        self._jm = getattr(V, "JM", None)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._jm is None:
            V.__dict__.pop("JM", None)
        else:
            V.JM = self._jm

    def test_no_prompt_bypasses_the_helper(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        helper = [node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name == "_prompt"]
        self.assertEqual(len(helper), 1, "缺少 _prompt 助手")
        inside_helper = {id(node) for node in ast.walk(helper[0])}
        bare = sorted(node.lineno for node in calls_named(tree, "input")
                      if id(node) not in inside_helper)
        self.assertEqual(bare, [], f"仍有直接 input() 调用: 行 {bare}")

    def test_every_prompt_uses_the_helper(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(calls_named(tree, "_prompt")), 30,
                                "提示数量异常，可能被改回 input()")

    def test_worker_mode_returns_the_default_without_touching_stdin(self):
        V.JM = True
        with mock.patch("sys.stdin", DeadStdin(AssertionError("worker 模式不应读取 stdin"))):
            self.assertEqual(payload._prompt("请输入："), "")
            self.assertEqual(payload._prompt("请输入：", default="7"), "7")

    def test_closed_stdin_returns_the_default(self):
        V.JM = False
        with mock.patch("sys.stdin", DeadStdin(EOFError())):
            self.assertEqual(payload._prompt("请输入："), "")
            self.assertEqual(payload._prompt("请输入：", default="1"), "1")

    def test_cli_mode_still_reads_a_line(self):
        V.JM = False

        class Piped:
            def __init__(self):
                self.lines = iter(["4096\n"])

            def readline(self):
                return next(self.lines)

            def isatty(self):
                return False

        with mock.patch("sys.stdin", Piped()):
            self.assertEqual(payload._prompt("请输入：").strip(), "4096")


if __name__ == "__main__":
    unittest.main()
