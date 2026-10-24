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
                             "memory_index_search", "memory_capture",
                             "thread_create", "thread_post", "thread_list", "thread_read",
                             "thread_fetch", "thread_ack", "thread_close", "thread_revoke"} <= names)
            self.assertNotIn("brain_apply", names)


if __name__ == "__main__":
    unittest.main()
