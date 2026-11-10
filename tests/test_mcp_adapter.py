"""The optional stdio adapter must register against the pinned MCP 2.x API."""

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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
                             "memory_index_search", "memory_search_verified", "memory_capture", "memory_capture_readiness",
                             "memory_capture_body_readiness", "memory_literal_verified", "read_successor_evidence",
                             "hindsight_sources_verify",
                             "thread_create", "thread_post", "thread_list", "thread_read",
                             "thread_fetch", "thread_ack", "thread_close", "thread_revoke"} <= names)
            self.assertNotIn("brain_apply", names)

    def test_hindsight_sources_use_authorized_read(self):
        from mcp.server.mcpserver import MCPServer
        body = '# Fictional source\n'
        revision = hashlib.sha256(body.encode()).hexdigest()
        path = 'Memory/notes/example.md'
        with tempfile.TemporaryDirectory(prefix='sios-mcp-') as raw:
            root = Path(raw)
            cfg = Config('client', '00000000-0000-4000-8000-000000000001', root/'data', root/'cache',
                         'UTC', 'https://127.0.0.1:9443', root/'client.json')
            with patch('aios.mcp_adapter.Client') as client, patch.object(MCPServer, 'run', autospec=True) as start:
                client.return_value.call.return_value = {'path': path, 'revision': revision, 'text': body}
                run(cfg)
                result = asyncio.run(start.call_args.args[0].call_tool('hindsight_sources_verify',
                    {'candidates': [{'path': path, 'revision': revision}]}))
            self.assertFalse(result.is_error)
            self.assertEqual(json.loads(result.content[0].text)['results'][0]['status'], 'verified')
            client.return_value.call.assert_called_once_with('read', {'relative': path})

    def test_capture_readiness_uses_only_existing_read_calls(self):
        from mcp.server.mcpserver import MCPServer
        stem = 'selective-' + 'a' * 64
        path = 'Memory/notes/' + stem + '.md'
        body = '# Fictional record\n'
        revision = hashlib.sha256(body.encode()).hexdigest()
        status = {'total_files': 1, 'observed_files': [{'path': 'notes/' + stem + '.md', 'checksum': revision}]}
        search = {'results': [{'title': stem, 'type': 'entity', 'file_path': 'notes/' + stem + '.md', 'read_path': path}],
                  'total': 1, 'has_more': False, 'current_page': 1, 'page_size': 10, 'total_is_exact': True}
        responses = {'health': {'ok': True, 'database': 'ok'}, 'memory_index_status': status,
                     'memory_index_search': search, 'read': {'path': path, 'revision': revision, 'text': body}}
        with tempfile.TemporaryDirectory(prefix='sios-mcp-') as raw:
            root = Path(raw)
            cfg = Config('client', '00000000-0000-4000-8000-000000000001', root/'data', root/'cache',
                         'UTC', 'https://127.0.0.1:9443', root/'client.json')
            with patch('aios.mcp_adapter.Client') as client, patch.object(MCPServer, 'run', autospec=True) as start:
                client.return_value.call.side_effect = lambda op, params: responses[op]
                run(cfg)
                result = asyncio.run(start.call_args.args[0].call_tool(
                    'memory_capture_readiness', {'path': path, 'revision': revision}))
            self.assertFalse(result.is_error)
            self.assertTrue(json.loads(result.content[0].text)['ready'])
            self.assertEqual([item.args[0] for item in client.return_value.call.call_args_list],
                             ['health', 'memory_index_status', 'memory_index_search', 'read', 'memory_index_status'])

    def test_body_literal_and_successor_tools_use_scoped_connection(self):
        from mcp.server.mcpserver import MCPServer
        stem = 'selective-' + 'a' * 64
        path = 'Memory/notes/' + stem + '.md'
        body = '<!-- aios-selective-memory-v1 -->\n```json\n{"supersedes": null}\n```\nfictionalbodytoken'
        revision = hashlib.sha256(body.encode()).hexdigest()
        hit = {'title': stem, 'type': 'entity', 'file_path': 'notes/' + stem + '.md', 'read_path': path}
        page = {'results': [hit], 'total': 1, 'has_more': False, 'current_page': 1, 'page_size': 10, 'total_is_exact': True}
        source = {'path': path, 'revision': revision, 'text': body}
        def call(op, args):
            if op == 'health': return {'ok': True, 'database': 'ok', 'pending_operations': 0}
            if op == 'memory_index_search': return page
            if op == 'search': return [{'path': path, 'revision': revision}]
            if op == 'read': return source
            raise AssertionError('unexpected operation')
        with tempfile.TemporaryDirectory(prefix='sios-mcp-') as raw:
            root = Path(raw)
            cfg = Config('client', '00000000-0000-4000-8000-000000000001', root/'data', root/'cache',
                         'UTC', 'https://127.0.0.1:9443', root/'client.json')
            with patch('aios.mcp_adapter.Client') as client, patch.object(MCPServer, 'run', autospec=True) as start:
                client.return_value.call.side_effect = call
                run(cfg)
                server = start.call_args.args[0]
                async def check():
                    body_result = await server.call_tool('memory_capture_body_readiness', {'path': path, 'revision': revision, 'marker': 'fictionalbodytoken'})
                    literal_result = await server.call_tool('memory_literal_verified', {'query': 'fictionalbodytoken'})
                    successor_result = await server.call_tool('read_successor_evidence', {'predecessor_path': path})
                    return body_result, literal_result, successor_result
                body_result, literal_result, successor_result = asyncio.run(check())
            self.assertTrue(json.loads(body_result.content[0].text)['ready'])
            self.assertEqual(len(json.loads(literal_result.content[0].text)['verified']), 1)
            self.assertEqual(json.loads(successor_result.content[0].text)['current_state'], 'unknown')
            self.assertTrue(set(x.args[0] for x in client.return_value.call.call_args_list) <=
                            {'health', 'memory_index_search', 'search', 'read'})

    def test_new_helpers_do_not_bypass_denied_read_scope(self):
        from aios.client import RemoteError
        from mcp.server.mcpserver import MCPServer
        stem = 'selective-' + 'a' * 64
        path = 'Memory/notes/' + stem + '.md'
        with tempfile.TemporaryDirectory(prefix='sios-mcp-') as raw:
            root = Path(raw)
            cfg = Config('client', '00000000-0000-4000-8000-000000000001', root/'data', root/'cache',
                         'UTC', 'https://127.0.0.1:9443', root/'client.json')
            def call(op, params):
                if op == 'health': return {'ok': True, 'database': 'ok', 'pending_operations': 0}
                raise RemoteError('forbidden', 403)
            with patch('aios.mcp_adapter.Client') as client, patch.object(MCPServer, 'run', autospec=True) as start:
                client.return_value.call.side_effect = call
                run(cfg)
                server = start.call_args.args[0]
                async def check():
                    body = await server.call_tool('memory_capture_body_readiness', {'path': path, 'revision': 'b'*64, 'marker': 'fictional'})
                    successor = await server.call_tool('read_successor_evidence', {'predecessor_path': path})
                    return body, successor
                body, successor = asyncio.run(check())
            self.assertFalse(json.loads(body.content[0].text)['ready'])
            self.assertEqual(json.loads(successor.content[0].text)['status'], 'unknown')
            self.assertTrue(set(x.args[0] for x in client.return_value.call.call_args_list) <=
                            {'health', 'memory_index_search', 'read'})

    def test_literal_scan_refuses_pending_operations(self):
        from mcp.server.mcpserver import MCPServer
        with tempfile.TemporaryDirectory(prefix='sios-mcp-') as raw:
            root = Path(raw)
            cfg = Config('client', '00000000-0000-4000-8000-000000000001', root/'data', root/'cache',
                         'UTC', 'https://127.0.0.1:9443', root/'client.json')
            with patch('aios.mcp_adapter.Client') as client, patch.object(MCPServer, 'run', autospec=True) as start:
                client.return_value.call.return_value = {'ok': True, 'pending_operations': 1}
                run(cfg)
                from mcp.server.mcpserver.exceptions import ToolError
                with self.assertRaises(ToolError):
                    asyncio.run(start.call_args.args[0].call_tool('memory_literal_verified', {'query': 'fiction'}))
            self.assertEqual([x.args[0] for x in client.return_value.call.call_args_list], ['health'])

    def test_proofs_have_separate_short_timeout_client(self):
        from mcp.server.mcpserver import MCPServer
        with tempfile.TemporaryDirectory(prefix='sios-mcp-') as raw:
            root = Path(raw)
            cfg = Config('client', '00000000-0000-4000-8000-000000000001', root/'data', root/'cache',
                         'UTC', 'https://127.0.0.1:9443', root/'client.json')
            normal, proof = Mock(), Mock()
            proof.call.return_value = {'ok': False}
            with patch('aios.mcp_adapter.Client', side_effect=[normal, proof]) as factory, \
                 patch.object(MCPServer, 'run', autospec=True) as start:
                run(cfg)
                result = asyncio.run(start.call_args.args[0].call_tool('memory_capture_body_readiness',
                    {'path': 'Memory/notes/selective-' + 'a'*64 + '.md', 'revision': 'b'*64, 'marker': 'fiction'}))
            self.assertFalse(json.loads(result.content[0].text)['ready'])
            self.assertEqual(factory.call_args_list[1].kwargs['timeout'], 6)
            self.assertEqual(proof.call.call_args.args[0], 'health')
            normal.call.assert_not_called()

    def test_failed_proof_client_does_not_disable_capture(self):
        from mcp.server.mcpserver import MCPServer
        with tempfile.TemporaryDirectory(prefix='sios-mcp-') as raw:
            root = Path(raw)
            cfg = Config('client', '00000000-0000-4000-8000-000000000001', root/'data', root/'cache',
                         'UTC', 'https://127.0.0.1:9443', root/'client.json')
            normal = Mock()
            normal.call.return_value = {'status': 'skipped'}
            with patch('aios.mcp_adapter.Client', side_effect=[normal, RuntimeError('synthetic private detail')]), \
                 patch.object(MCPServer, 'run', autospec=True) as start:
                run(cfg)
                result = asyncio.run(start.call_args.args[0].call_tool('memory_capture', {'request_json': '{"retain":false}'}))
                proof = asyncio.run(start.call_args.args[0].call_tool('memory_capture_body_readiness',
                    {'path': 'Memory/notes/selective-' + 'a'*64 + '.md', 'revision': 'b'*64, 'marker': 'fiction'}))
            self.assertEqual(json.loads(result.content[0].text)['status'], 'skipped')
            self.assertFalse(json.loads(proof.content[0].text)['ready'])
            self.assertNotIn('synthetic private detail', str(proof))
            self.assertEqual([x.args[0] for x in normal.call.call_args_list], ['memory_capture'])

    def test_refresh_rejects_path_without_sending_request_or_echoing_input(self):
        from mcp.server.mcpserver import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError
        with tempfile.TemporaryDirectory(prefix='sios-mcp-') as raw:
            root = Path(raw)
            cfg = Config('client', '00000000-0000-4000-8000-000000000001', root/'data', root/'cache',
                         'UTC', 'https://127.0.0.1:9443', root/'client.json')
            with patch('aios.mcp_adapter.Client') as client, patch.object(MCPServer, 'run', autospec=True) as start:
                run(cfg)
                for invalid in ('Memory/notes/fiction.md', 'fiction.md', 'UPPER', 'bad name', 'con', '../x'):
                    with self.subTest(invalid=invalid), self.assertRaises(ToolError) as caught:
                        asyncio.run(start.call_args.args[0].call_tool('memory_index_refresh',
                            {'identifier': invalid, 'expected_revision': 'a'*64, 'query': 'fiction'}))
                    self.assertIn('invalid_identifier', str(caught.exception))
                    self.assertNotIn(invalid, str(caught.exception))
                client.return_value.call.assert_not_called()
                client.return_value.call.return_value = {'ready': False}
                response = asyncio.run(start.call_args.args[0].call_tool('memory_index_refresh',
                    {'identifier': 'fiction', 'expected_revision': 'a'*64, 'query': 'fiction'}))
                self.assertFalse(response.is_error)
                self.assertEqual(client.return_value.call.call_args.args[1]['identifier'], 'fiction')

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
