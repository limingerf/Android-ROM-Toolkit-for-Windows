"""Protocol-stream and RangeSet truthiness regressions.

The MCP stdio server writes JSON-RPC to stdout, and library code prints there
too (the native toolchain bootstrap reports its progress with ``print``), so a
single stray line used to be injected between protocol messages and made the
client drop the session.

``RangeSet`` only defined the Python 2 spelling ``__nonzero__``, so on Python 3
every instance - including an empty one - was truthy.
"""
import contextlib
import io
import json
import unittest
from pathlib import Path

from Scripts import mcp_server
from Scripts.Primary.RangeModule import RangeSet

ROOT = str(Path(mcp_server.__file__).resolve().parent.parent)


class McpStdoutHygieneTests(unittest.TestCase):
    def test_tool_prints_never_reach_the_protocol_stream(self):
        server = mcp_server.McpServer(ROOT)

        def noisy_tool(name, args):
            # This is what Toolchain.resolve() does while bootstrapping avbroot.
            print("native avbroot bootstrap: downloading ...", flush=True)
            return {"status": "ok"}

        server.call = noisy_tool
        request = {
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "art_ota_status", "arguments": {}},
        }
        protocol = io.StringIO()
        diagnostics = io.StringIO()
        with contextlib.redirect_stdout(protocol), contextlib.redirect_stderr(diagnostics):
            response = server.handle(request)
        self.assertEqual(response["result"], {"status": "ok"})
        self.assertEqual(protocol.getvalue(), "", "stdout carries only JSON-RPC")
        self.assertIn("bootstrap", diagnostics.getvalue())

    def test_protocol_response_is_still_one_json_line(self):
        server = mcp_server.McpServer(ROOT)
        server.call = lambda name, args: {"echo": name}
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "art_list_projects", "arguments": {}},
        }
        protocol = io.StringIO()
        with contextlib.redirect_stdout(protocol):
            response = server.handle(request)
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(encoded.count("\n"), 0)
        self.assertEqual(response["result"], {"echo": "art_list_projects"})

    def test_every_fastmcp_tool_is_registered_through_the_guard(self):
        source = (mcp_server.__file__ or "")
        with open(source, encoding="utf-8") as stream:
            text = stream.read()
        self.assertIn("def quiet_tool(", text)
        self.assertEqual(text.count("@server.tool()"), 0)
        self.assertEqual(text.count("@quiet_tool()"), 27)


class RangeSetTruthinessTests(unittest.TestCase):
    def test_empty_range_set_is_falsy(self):
        self.assertFalse(bool(RangeSet()))

    def test_non_empty_range_set_is_truthy(self):
        self.assertTrue(bool(RangeSet([0, 1])))


if __name__ == "__main__":
    unittest.main()
