"""Synthetic integration test verifying search resolver through real MCP ClientSession and MCPServer.

Uses authentic MCP JSON-RPC client/server protocol over in-memory bidirectional streams.
All notes and Store states are strictly isolated synthetic fixtures in temporary directories.
Zero modifications or connections to production service or production data.
Zero Linux/VM dependencies; native macOS Python.
"""
from __future__ import annotations

import anyio
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from mcp.client.session import ClientSession
from mcp.server.mcpserver import MCPServer

from aios.search_resolver import resolve_search_verified, resolve_search
from aios.store import Store


def _format_note(stem: str, meaning_id: str, claim: str, conditions: list[str], supersedes: str | None = None) -> str:
    body = {
        "schema": "aios-selective-memory-v1",
        "meaning_id": meaning_id,
        "claim": claim,
        "conditions": conditions,
        "supersedes": supersedes,
        "evidence_status": "verified",
        "recorded_at": "2026-11-01T00:00:00Z",
    }
    json_str = json.dumps(body, indent=2)
    return (
        f"---\ntitle: {stem}\ntype: note\npermalink: aios/notes/{stem}\n---\n\n"
        f"<!-- aios-selective-memory-v1 -->\n# Selective Memory: {meaning_id}\n\n"
        f"```json\n{json_str}\n```\n\n{claim}\n"
    )


class TestSearchResolverRealMCPIntegration(unittest.TestCase):
    def setUp(self):
        self.raw_temp = tempfile.mkdtemp(prefix="sios-test-mcp-")
        self.root = Path(self.raw_temp).resolve()
        (self.root / "Memory" / "notes").mkdir(parents=True, exist_ok=True)
        (self.root / "Database").mkdir(parents=True, exist_ok=True)
        self.store = Store(self.root)
        self.fts_data: dict[str, list[dict]] = {}

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write_note(self, stem: str, meaning_id: str, claim: str, conditions: list[str], supersedes: str | None = None) -> str:
        path = f"Memory/notes/{stem}.md"
        content = _format_note(stem, meaning_id, claim, conditions, supersedes)
        res = self.store._write("memory", path, content, f"req-{stem[-8:]}", None, True, "synthetic-actor")
        return res["result"]["revision"]

    def _build_mcp_server(self) -> MCPServer:
        server = MCPServer("synthetic-sios-client")

        @server.tool(name="read")
        def read(relative: str) -> dict:
            return self.store.read(relative)

        @server.tool(name="memory_index_search")
        def memory_index_search(query: str) -> dict:
            hits = self.fts_data.get(query, [])
            return {"results": hits, "has_more": False, "total": len(hits)}

        return server

    def test_real_mcp_connection_resolves_pointer_and_missing_fts(self):
        """Execute search resolver over authentic MCP ClientSession JSON-RPC tool calls."""
        # Note A: Missing from FTS
        doc_a_stem = "selective-" + "a" * 64
        rev_a = self._write_note(doc_a_stem, "original-note-a", "Authoritative original content A.", ["cond_a"])

        # Note B: Pointer note indexed in FTS that references Note A
        doc_b_stem = "selective-" + "b" * 64
        claim_b = f"Pointer note referencing Memory/notes/{doc_a_stem}.md directly."
        rev_b = self._write_note(doc_b_stem, "pointer-note-b", claim_b, ["cond_b"], supersedes=doc_a_stem)

        # Populate synthetic FTS index: query 'design' returns only Note B
        self.fts_data["design"] = [
            {
                "title": doc_b_stem,
                "read_path": f"Memory/notes/{doc_b_stem}.md",
                "score": -5.0,
                "content": f"FTS snippet referencing {doc_a_stem}",
            }
        ]

        server = self._build_mcp_server()

        async def run_mcp_session():
            client_send, server_receive = anyio.create_memory_object_stream(10)
            server_send, client_receive = anyio.create_memory_object_stream(10)

            init_opts = server._lowlevel_server.create_initialization_options()
            async with anyio.create_task_group() as tg:
                tg.start_soon(server._lowlevel_server.run, server_receive, server_send, init_opts)
                async with ClientSession(client_receive, client_send) as session:
                    await session.initialize()

                    # Verify real MCP tool registration
                    tool_list = await session.list_tools()
                    tool_names = {t.name for t in tool_list.tools}
                    self.assertIn("read", tool_names)
                    self.assertIn("memory_index_search", tool_names)

                    # Wrap MCP call_tool into synchronous callables for search_resolver
                    def mcp_read(relative: str) -> dict:
                        # Call tool over authentic MCP session
                        res = anyio.from_thread.run(session.call_tool, "read", {"relative": relative})
                        text = res.content[0].text
                        return json.loads(text)

                    def mcp_search(query: str) -> dict:
                        res = anyio.from_thread.run(session.call_tool, "memory_index_search", {"query": query})
                        text = res.content[0].text
                        return json.loads(text)

                    # Run resolver in worker thread so anyio.from_thread can call back into session
                    def run_resolver():
                        return resolve_search_verified("design", mcp_search, mcp_read, max_reads=6)

                    result = await anyio.to_thread.run_sync(run_resolver)

                    # Verify results returned across real MCP connection
                    self.assertEqual(result["total_results"], 2)
                    self.assertEqual(result["referring_verified_count"], 1)
                    self.assertEqual(result["targets_verified_count"], 1)

                    hit_b = result["results"][0]
                    self.assertEqual(hit_b["path"], f"Memory/notes/{doc_b_stem}.md")
                    self.assertEqual(hit_b["revision"], rev_b)
                    self.assertEqual(hit_b["status"], "verified_referring_source")

                    hit_a = result["results"][1]
                    self.assertEqual(hit_a["path"], f"Memory/notes/{doc_a_stem}.md")
                    self.assertEqual(hit_a["revision"], rev_a)
                    self.assertEqual(hit_a["status"], "resolved_target_original")
                    self.assertEqual(hit_a["relationship_to_pointer"], "referring_source_supersedes_target")
                    self.assertEqual(hit_a["structured_record"]["meaning_id"], "original-note-a")

                tg.cancel_scope.cancel()

        anyio.run(run_mcp_session)

    def test_real_mcp_exact_stem_direct_resolution(self):
        """Execute exact-stem fallback on FTS-omitted note over authentic MCP session."""
        doc_c_stem = "selective-" + "c" * 64
        rev_c = self._write_note(doc_c_stem, "original-note-c", "Authoritative content C.", ["cond_c"])

        server = self._build_mcp_server()

        async def run_mcp_exact_session():
            client_send, server_receive = anyio.create_memory_object_stream(10)
            server_send, client_receive = anyio.create_memory_object_stream(10)

            init_opts = server._lowlevel_server.create_initialization_options()
            async with anyio.create_task_group() as tg:
                tg.start_soon(server._lowlevel_server.run, server_receive, server_send, init_opts)
                async with ClientSession(client_receive, client_send) as session:
                    await session.initialize()

                    def mcp_read(relative: str) -> dict:
                        res = anyio.from_thread.run(session.call_tool, "read", {"relative": relative})
                        return json.loads(res.content[0].text)

                    def mcp_search(query: str) -> dict:
                        res = anyio.from_thread.run(session.call_tool, "memory_index_search", {"query": query})
                        return json.loads(res.content[0].text)

                    def run_resolver():
                        return resolve_search_verified(doc_c_stem, mcp_search, mcp_read, max_reads=4)

                    result = await anyio.to_thread.run_sync(run_resolver)

                    self.assertTrue(result["exact_stem_resolved"])
                    self.assertEqual(result["total_results"], 1)
                    hit = result["results"][0]
                    self.assertEqual(hit["path"], f"Memory/notes/{doc_c_stem}.md")
                    self.assertEqual(hit["revision"], rev_c)
                    self.assertEqual(hit["status"], "verified_exact_source")

                tg.cancel_scope.cancel()

        anyio.run(run_mcp_exact_session)


if __name__ == "__main__":
    unittest.main()
