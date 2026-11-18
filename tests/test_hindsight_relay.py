import io
import hashlib
import json
import os
import ssl
import shutil
import unittest
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError
from pathlib import Path

from aios.hindsight_relay import Relay, _NoRedirect, handle


class Response:
    def __init__(self, body):
        self.body = body
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self, n):
        return self.body[:n]


class RelayTests(unittest.TestCase):
    def make_relay(self):
        with patch('aios.hindsight_relay.ssl.SSLContext', return_value=MagicMock()), \
             patch('aios.hindsight_relay.Path.open', return_value=io.BytesIO(b'fixture')), \
             patch('aios.hindsight_relay.urllib.request.build_opener') as build:
            relay = Relay('https://example.test:8443', '/fixture/ca.pem', hashlib.sha256(b'fixture').hexdigest())
        return relay, build.return_value

    def test_requires_https_clean_origin_and_explicit_ca(self):
        for endpoint in ('http://example.test', 'https://user' + '@' + 'example.test',
                         'https://example.test/path', 'https://example.test/?x=1',
                         'https://example.test/#x', 'https://example.test:bad'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                Relay(endpoint, '/fixture/ca.pem', 'a' * 64)
        with self.assertRaises(ValueError):
            Relay('https://example.test', None, 'a' * 64)

    def test_redirect_is_never_followed(self):
        handler = _NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, '', {}, 'https://other.test/'))

    def test_wrong_ca_pin_is_rejected_before_context_creation(self):
        with patch('aios.hindsight_relay.Path.open', return_value=io.BytesIO(b'fixture')), \
             patch('aios.hindsight_relay.ssl.SSLContext') as context:
            with self.assertRaisesRegex(ValueError, 'ca_pin_mismatch'):
                Relay('https://example.test', '/fixture/ca.pem', 'a' * 64)
            context.assert_not_called()

    def test_forwards_once_and_preserves_gateway_result(self):
        relay, opener = self.make_relay()
        opener.open.return_value = Response(json.dumps({'ok': True, 'result': {'status': 'ok'}}).encode())
        with patch.dict(os.environ, {'SIOS_MEMORY_GATEWAY_TOKEN': 'fixture-token'}):
            error, value = relay.forward('memory_recall', {'query': 'example', 'project_tag': 'project:test'})
        self.assertFalse(error)
        self.assertEqual(value, {'status': 'ok'})
        self.assertEqual(opener.open.call_count, 1)
        req = opener.open.call_args.args[0]
        self.assertEqual(req.full_url, 'https://example.test:8443/v1/memory_recall')
        self.assertEqual(req.get_header('Authorization'), 'Bearer fixture-token')

    def test_redirect_error_does_not_repeat_write(self):
        relay, opener = self.make_relay()
        opener.open.side_effect = HTTPError('https://example.test', 307, 'redirect', {}, io.BytesIO(b''))
        with patch.dict(os.environ, {'SIOS_MEMORY_GATEWAY_TOKEN': 'fixture-token'}):
            error, value = relay.forward('memory_store', {'request_id': 'fixture-123'})
        self.assertTrue(error)
        self.assertEqual(value, {'error': 'unknown_no_retry'})
        self.assertEqual(opener.open.call_count, 1)

    def test_all_http_errors_on_store_are_uncertain(self):
        for status in (400, 403, 408, 429, 500):
            relay, opener = self.make_relay()
            opener.open.side_effect = HTTPError('https://example.test', status, '', {}, io.BytesIO(b''))
            with patch.dict(os.environ, {'SIOS_MEMORY_GATEWAY_TOKEN': 'fixture-token'}):
                self.assertEqual(relay.forward('memory_store', {'request_id': 'fixture-123'})[1]['error'], 'unknown_no_retry')
            self.assertEqual(opener.open.call_count, 1)

    def test_response_limit_and_static_error(self):
        relay, opener = self.make_relay()
        opener.open.return_value = Response(b'x' * 1048577)
        with patch.dict(os.environ, {'SIOS_MEMORY_GATEWAY_TOKEN': 'fixture-token'}):
            self.assertEqual(relay.forward('memory_recall', {})[1]['error'], 'gateway_response_too_large')
            opener.open.side_effect = HTTPError('https://example.test', 500, '', {}, io.BytesIO(b'{"error":"private detail"}'))
            self.assertEqual(relay.forward('memory_recall', {})[1]['error'], 'gateway_http_500')

    def test_unknown_tool_never_reaches_network(self):
        relay, opener = self.make_relay()
        self.assertEqual(relay.forward('other', {})[1]['error'], 'invalid_tool_call')
        opener.open.assert_not_called()

    def test_malformed_params_return_error_without_crashing(self):
        relay, opener = self.make_relay()
        output = io.StringIO()
        with patch('sys.stdout', output):
            handle(relay, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': 'bad'})
        self.assertEqual(json.loads(output.getvalue())['error']['code'], -32602)
        opener.open.assert_not_called()

    def test_invalid_envelope_and_argument_types_are_rejected(self):
        relay, opener = self.make_relay()
        for msg in ({'id': 1, 'method': 'ping'}, {'jsonrpc': '2.0', 'id': [], 'method': 'ping'},
                    {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                     'params': {'name': 'memory_recall', 'arguments': []}},
                    {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                     'params': {'name': [], 'arguments': {}}}):
            output = io.StringIO()
            with patch('sys.stdout', output):
                handle(relay, msg)
            self.assertIn('error', json.loads(output.getvalue()))
        opener.open.assert_not_called()

    def test_ambiguous_store_response_is_never_retried(self):
        relay, opener = self.make_relay()
        opener.open.return_value = Response(b'not json')
        with patch.dict(os.environ, {'SIOS_MEMORY_GATEWAY_TOKEN': 'fixture-token'}):
            self.assertEqual(relay.forward('memory_store', {'request_id': 'fixture-123'})[1]['error'], 'unknown_no_retry')
        self.assertEqual(opener.open.call_count, 1)


if __name__ == '__main__':
    unittest.main()

@unittest.skipUnless(shutil.which('openssl'), 'OpenSSL is required for synthetic TLS integration')
class GatewayIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import http.server
        import subprocess
        import tempfile
        import threading
        from pathlib import Path
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        ext = root / 'ext.cnf'
        ext.write_text('subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth\nauthorityKeyIdentifier=keyid\n')
        def run(*args):
            subprocess.run(args, cwd=root, check=True, capture_output=True)
        run('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'ca.key', '-out', 'ca.crt', '-subj', '/CN=Fixture CA', '-days', '1', '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign', '-addext', 'subjectKeyIdentifier=hash')
        run('openssl', 'req', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'server.key', '-out', 'server.csr', '-subj', '/CN=fixture')
        run('openssl', 'x509', '-req', '-in', 'server.csr', '-CA', 'ca.crt', '-CAkey', 'ca.key', '-CAcreateserial', '-out', 'server.crt', '-days', '1', '-extfile', 'ext.cnf')
        class Handler(http.server.BaseHTTPRequestHandler):
            requests = []
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                self.requests.append((self.path, self.headers.get('Authorization'), json.loads(body)))
                response = json.dumps({'ok': True, 'result': {'status': 'fixture'}}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(response)))
                self.end_headers()
                self.wfile.write(response)
            def log_message(self, *args):
                pass
        cls.handler = Handler
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(root / 'server.crt', root / 'server.key')
        cls.server.socket = ctx.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.ca = str(root / 'ca.crt')
        cls.port = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def test_https_gateway_roundtrip(self):
        relay = Relay(f'https://127.0.0.1:{self.port}', self.ca, hashlib.sha256(Path(self.ca).read_bytes()).hexdigest())
        self.assertEqual(relay.ctx.cert_store_stats()['x509_ca'], 1)
        with patch.dict(os.environ, {'SIOS_MEMORY_GATEWAY_TOKEN': 'fixture-token'}):
            error, result = relay.forward('memory_recall', {'query': 'fixture', 'project_tag': 'project:test'})
        self.assertFalse(error)
        self.assertEqual(result, {'status': 'fixture'})
        self.assertEqual(self.handler.requests[-1], ('/v1/memory_recall', 'Bearer fixture-token', {'query': 'fixture', 'project_tag': 'project:test'}))

    def test_wrong_hostname_refused(self):
        relay = Relay(f'https://localhost:{self.port}', self.ca, hashlib.sha256(Path(self.ca).read_bytes()).hexdigest())
        with patch.dict(os.environ, {'SIOS_MEMORY_GATEWAY_TOKEN': 'fixture-token'}):
            self.assertEqual(relay.forward('memory_recall', {})[1]['error'], 'gateway_unavailable')
