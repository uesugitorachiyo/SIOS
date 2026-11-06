"""The optional stdio adapter must register against the pinned MCP 2.x API."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aios.config import Config
from aios.mcp_adapter import run


class MCPAdapterTests(unittest.TestCase):
    def test_client_tools_register_with_pinned_mcp(self):
        try:
            from mcp.server.mcpserver import MCPServer
        except ImportError:
            self.skipTest("optional MCP dependency is not installed")

        with tempfile.TemporaryDirectory(prefix="sios-mcp-") as raw:
            root = Path(raw)
            cfg = Config("client", "00000000-0000-4000-8000-000000000001",
                         root / "data", root / "cache", "UTC",
                         "https://127.0.0.1:9443", root / "client.json")
            with patch("aios.mcp_adapter.Client"), patch.object(MCPServer, "run", autospec=True) as start:
                run(cfg)

            start.assert_called_once()
            self.assertEqual(start.call_args.kwargs, {"transport": "stdio"})
            server = start.call_args.args[0]
            names = {tool.name for tool in asyncio.run(server.list_tools())}
            self.assertTrue({"identity", "read", "catalog", "memory_write", "memory_index_refresh",
                             "memory_index_search", "memory_search_verified", "memory_capture",
                             "thread_create", "thread_post", "thread_list", "thread_read",
                             "thread_fetch", "thread_ack", "thread_close", "thread_revoke"} <= names)
            self.assertNotIn("brain_apply", names)

    def test_verified_search_uses_registered_connection(self):
        from mcp.server.mcpserver import MCPServer
        with tempfile.TemporaryDirectory(prefix="sios-mcp-") as raw:
            root = Path(raw)
            cfg = Config("client", "00000000-0000-4000-8000-000000000001",
                         root / "data", root / "cache", "UTC",
                         "https://127.0.0.1:9443", root / "client.json")
            with patch("aios.mcp_adapter.Client") as client, patch.object(MCPServer, "run", autospec=True) as start:
                client.return_value.call.side_effect = lambda op, args: (
                    {"ok": True} if op == "health" else {"results": []})
                run(cfg)
                response = asyncio.run(start.call_args.args[0].call_tool(
                    "memory_search_verified", {"query": "fictional", "max_reads": 0}))
            self.assertFalse(response.is_error)
            self.assertEqual([item.args[0] for item in client.return_value.call.call_args_list],
                             ["health", "memory_index_search"])


if __name__ == "__main__":
    unittest.main()
