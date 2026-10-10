"""JSON-RPC handling of the stdio MCP server.

A request without a usable ``method`` used to raise AttributeError inside
``handle``, which ``serve`` turned into a parse error (-32700) with a null id:
the client waited forever for the id it had sent, and the wrong code told it to
blame the transport instead of its own request.
"""
import tempfile
import unittest

from Scripts.mcp_server import McpServer


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.server = McpServer(self._tmp.name)

    def test_missing_method_is_an_invalid_request_with_the_client_id(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 5})
        self.assertEqual(response["id"], 5)
        self.assertEqual(response["error"]["code"], -32600)

    def test_non_string_method_is_an_invalid_request(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 6, "method": 123})
        self.assertEqual(response["id"], 6)
        self.assertEqual(response["error"]["code"], -32600)

    def test_non_object_request_is_an_invalid_request(self):
        for payload in ("initialize", [1, 2], 7, None):
            response = self.server.handle(payload)
            self.assertIsNone(response["id"], payload)
            self.assertEqual(response["error"]["code"], -32600, payload)

    def test_unknown_method_is_reported_as_method_not_found(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/nope"})
        self.assertEqual(response["error"]["code"], -32601)

    def test_notifications_get_no_response(self):
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled"}))

    def test_ping_and_tools_list_stay_side_effect_free(self):
        self.assertEqual(self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "ping"})["result"], {})
        listed = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertTrue(listed)
        self.assertEqual([tool["name"] for tool in listed][0], "art_toolchain_status")

    def test_unknown_tool_is_a_json_rpc_error_not_a_crash(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                       "params": {"name": "art_missing", "arguments": {}}})
        self.assertEqual(response["id"], 3)
        self.assertIn("未知工具", response["error"]["message"])


if __name__ == "__main__":
    unittest.main()
