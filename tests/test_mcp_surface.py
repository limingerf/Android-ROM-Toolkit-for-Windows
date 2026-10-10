"""Guards the published MCP tool surface.

``tools/list`` is served from ``TOOLS`` while ``tools/call`` dispatches through a
separate ``operations`` table, so the two can silently drift apart: a tool can be
advertised and then fail with an unknown-tool error, or a handler can become
unreachable.  The check is static (ast) so it needs neither the ``mcp`` package
nor a running server, and it also verifies that every ``required`` field is a
declared property -- clients validate against that schema before calling.
"""
import ast
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "Scripts" / "mcp_server.py"


def module_tree():
    return ast.parse(SOURCE.read_text(encoding="utf-8"))


def literal_fields(node):
    return {key.value: value for key, value in zip(node.keys, node.values)}


def tool_declarations(tree):
    """Return {tool name: declaration} from the module-level TOOLS list."""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "TOOLS" for target in node.targets):
            tools = {}
            for element in node.value.elts:
                fields = literal_fields(element)
                tools[fields["name"].value] = fields
            return tools
    raise AssertionError("mcp_server.py 中没有找到 TOOLS")


def dispatch_names(tree):
    """Return the string keys of the ``operations`` table inside McpServer.call."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "call":
            for statement in node.body:
                if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Dict):
                    return [key.value for key in statement.value.keys]
    raise AssertionError("McpServer.call 中没有找到 operations 表")


class McpSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.tree = module_tree()
        self.tools = tool_declarations(self.tree)
        self.dispatched = dispatch_names(self.tree)

    def test_tool_names_are_unique(self):
        self.assertEqual(len(self.tools), len(set(self.tools)))

    def test_every_advertised_tool_has_a_handler(self):
        missing = sorted(set(self.tools) - set(self.dispatched))
        self.assertEqual(missing, [], f"tools/list 中声明但无法调用: {missing}")

    def test_every_handler_is_advertised(self):
        extra = sorted(set(self.dispatched) - set(self.tools))
        self.assertEqual(extra, [], f"有处理函数但未声明: {extra}")

    def test_tool_names_are_namespaced(self):
        bad = sorted(name for name in self.tools if not name.startswith("art_"))
        self.assertEqual(bad, [], f"工具名缺少 art_ 前缀: {bad}")

    def test_schemas_declare_required_properties(self):
        for name, declaration in self.tools.items():
            schema = literal_fields(declaration["inputSchema"])
            self.assertEqual(schema["type"].value, "object", name)
            properties = {key.value for key in schema["properties"].keys}
            required = schema.get("required")
            if required is None:
                continue
            for entry in required.elts:
                self.assertIn(entry.value, properties,
                              f"{name} 要求了未声明的字段 {entry.value}")

    def test_every_tool_has_a_description(self):
        for name, declaration in self.tools.items():
            description = declaration.get("description")
            self.assertTrue(description is not None and description.value.strip(), name)


if __name__ == "__main__":
    unittest.main()
