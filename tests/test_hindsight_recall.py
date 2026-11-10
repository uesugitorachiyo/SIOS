import hashlib
import json
import asyncio
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from aios.hindsight_recall import recall_verified


class FixtureHandler(BaseHTTPRequestHandler):
    requests = []
    revision = hashlib.sha256(b'fictional original').hexdigest()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.__class__.requests.append((self.path, body))
        payload = {'results': [
            {'id': 'fixture', 'text': 'untrusted snippet', 'tags': ['project:fiction'], 'metadata': {
                'source_path': 'Memory/notes/fiction.md', 'source_revision': self.revision}},
            {'id': 'no-metadata', 'text': 'ignored'}]}
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class RecallTests(unittest.TestCase):
    def test_real_sdk_fictional_http_and_source_read(self):
        try:
            import hindsight_client  # noqa: F401
        except ImportError:
            self.skipTest('optional hindsight-client not installed')
        FixtureHandler.requests = []
        server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = recall_verified(url=f'http://127.0.0.1:{server.server_port}', bank='fiction',
                tag='project:fiction', query='fictional question',
                read=lambda path: {'path': path, 'revision': FixtureHandler.revision, 'text': 'fictional original'})
        finally:
            server.shutdown(); server.server_close(); thread.join()
        self.assertEqual(result['results'][0]['status'], 'verified')
        self.assertEqual(result['results'][0]['text'], 'fictional original')
        self.assertEqual(result['unusable_hits'], 1)
        self.assertNotIn('untrusted snippet', str(result))
        self.assertEqual(FixtureHandler.requests[0][0], '/v1/default/banks/fiction/memories/recall')
        self.assertEqual(FixtureHandler.requests[0][1]['tags'], ['project:fiction'])
        self.assertEqual(FixtureHandler.requests[0][1]['tags_match'], 'all_strict')

    def test_refuses_nonlocal_plain_http_before_network(self):
        with self.assertRaises(ValueError):
            recall_verified(url='http://example.com', bank='a', tag='project:a', query='q',
                            read=lambda _: self.fail('read called'),
                            client_factory=lambda **_: self.fail('network called'))

    def test_mcp_tool_calls_real_sdk_from_running_event_loop(self):
        try:
            from mcp.server.mcpserver import MCPServer
            import hindsight_client  # noqa: F401
        except ImportError:
            self.skipTest('optional dependencies not installed')
        from aios.config import Config
        from aios.mcp_adapter import run
        server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                cfg = Config('client', '00000000-0000-4000-8000-000000000001', root/'data', root/'cache',
                             'UTC', 'https://127.0.0.1:9443', root/'client.json')
                env = {'SIOS_HINDSIGHT_RECALL_ENABLED': '1', 'SIOS_HINDSIGHT_URL': f'http://127.0.0.1:{server.server_port}',
                       'SIOS_HINDSIGHT_BANK': 'fiction', 'SIOS_HINDSIGHT_TAG': 'project:fiction'}
                with patch.dict(os.environ, env), patch('aios.mcp_adapter.Client') as client, \
                     patch.object(MCPServer, 'run', autospec=True) as start:
                    client.return_value.call.return_value = {'path': 'Memory/notes/fiction.md',
                        'revision': FixtureHandler.revision, 'text': 'fictional original'}
                    run(cfg)
                    result = asyncio.run(start.call_args.args[0].call_tool('hindsight_recall_verified', {'query':'fiction'}))
                    self.assertFalse(result.is_error, str(result))
                    self.assertEqual(json.loads(result.content[0].text)['results'][0]['status'], 'verified')
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_no_implicit_bank_or_tag(self):
        for bank, tag in [('', 'project:a'), ('a', '')]:
            with self.assertRaises(ValueError):
                recall_verified(url='http://127.0.0.1', bank=bank, tag=tag, query='q',
                                read=lambda _: self.fail('read called'))

    def test_missing_tag_and_bad_pointer_are_discarded(self):
        class Client:
            def __init__(self, **kw): pass
            async def aclose(self): pass
            async def arecall(self, **kwargs):
                return SimpleNamespace(results=[
                    SimpleNamespace(tags=[], metadata={'source_path': 'Memory/notes/a.md', 'source_revision': 'a'*64}),
                    SimpleNamespace(tags=['project:a'], metadata={'source_path': 'Memory/../x.md', 'source_revision': 'a'*64})])
        result = recall_verified(url='http://127.0.0.1', bank='a', tag='project:a', query='q',
                                 read=lambda _: self.fail('untrusted read'), client_factory=Client)
        self.assertEqual(result['results'], [])
        self.assertEqual(result['unusable_hits'], 2)

    def test_remote_error_is_sanitized_without_retry(self):
        calls = []
        class Client:
            def __init__(self, **kw): self.kw = kw
            async def aclose(self): pass
            async def arecall(self, **kwargs):
                calls.append(self.kw['max_attempts'])
                raise RuntimeError('credential should not appear')
        with self.assertRaisesRegex(RuntimeError, '^hindsight_recall_unavailable$'):
            recall_verified(url='http://127.0.0.1', bank='a', tag='project:a', query='q',
                            read=lambda _: self.fail('unexpected read'), client_factory=Client)
        self.assertEqual(calls, [1])
