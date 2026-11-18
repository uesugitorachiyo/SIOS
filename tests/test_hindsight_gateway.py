"""Tests for hindsight_gateway.py and the Windows relay. Synthetic only: fake backend, temp CA, loopback."""
import json
import hashlib
import os
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
from aios import hindsight_gateway as gw
from aios.hindsight_relay import Relay
if os.name == 'posix':
    from aios.hindsight_backend import HindsightBackend
from tests.test_hindsight_backend import FakeTarget
from unittest.mock import patch


class FakeBackend:
    def __init__(self):
        self.calls = []

    def call(self, op, actor, args):
        self.calls.append((op, actor, args))
        if args.get('query') == 'boom':
            raise RuntimeError('secret-detail private-path')
        if args.get('query') == 'bad':
            raise ValueError('invalid_query')
        return {'op': op, 'actor': actor, 'echo': args}

    def health(self):
        return {'ok': True, 'bank': 'fake'}

    def map_error(self, exc):
        if isinstance(exc, ValueError):
            return 400, str(exc)
        return 500, 'internal_error'


def make_certs(d: Path):
    ext = d / 'ext.cnf'
    ext.write_text('subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth\nauthorityKeyIdentifier=keyid\n')
    run = lambda *a: subprocess.run(a, check=True, capture_output=True, cwd=d)
    run('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'ca.key', '-out', 'ca.crt',
        '-subj', '/CN=Test CA', '-days', '2', '-addext', 'basicConstraints=critical,CA:TRUE',
        '-addext', 'keyUsage=critical,keyCertSign', '-addext', 'subjectKeyIdentifier=hash')
    run('openssl', 'req', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'server.key', '-out', 'server.csr',
        '-subj', '/CN=127.0.0.1')
    run('openssl', 'x509', '-req', '-in', 'server.csr', '-CA', 'ca.crt', '-CAkey', 'ca.key', '-CAcreateserial',
        '-out', 'server.crt', '-days', '2', '-extfile', 'ext.cnf')


@unittest.skipUnless(os.name == 'posix', 'POSIX server gateway')
class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        os.chmod(d, 0o700)
        make_certs(d)
        os.chmod(d / 'server.key', 0o600)
        cls.ca = str(d / 'ca.crt')
        cfg_path = d / 'gateway.json'
        cfg = {'host': '127.0.0.1', 'port': 18991, 'cert_file': str(d / 'server.crt'),
               'key_file': str(d / 'server.key'), 'devices': {}}
        gw._save_config_locked(cfg_path, cfg)
        cls.rw, cls.ro = d / 'rw.token', d / 'ro.token'
        gw.init_device(cfg_path, 'win-rw', ['read', 'memory:write'], cls.rw)
        gw.init_device(cfg_path, 'win-ro', ['read'], cls.ro)
        cls.rw_token, cls.ro_token = cls.rw.read_text(), cls.ro.read_text()
        cls.cfg = gw.load_config(cfg_path)
        cls.backend = FakeBackend()
        cls.server = gw.Gateway(cls.cfg, cls.backend, ssl_context=gw.build_ssl_context(cls.cfg))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.ctx = ssl.create_default_context(cafile=cls.ca)
        cls.base = 'https://127.0.0.1:18991'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def req(self, method, path, token=None, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(r, context=self.ctx, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_token_file_mode_and_config_has_only_digest(self):
        self.assertEqual(oct(self.rw.stat().st_mode & 0o777), '0o600')
        cfg_text = (Path(self.tmp.name) / 'gateway.json').read_text()
        self.assertNotIn(self.rw_token, cfg_text)
        self.assertIn(gw.token_digest(self.rw_token), cfg_text)

    def test_config_save_does_not_reuse_permissive_predictable_temp(self):
        root = Path(self.tmp.name)
        target = root / 'other-config.json'
        stale = Path(str(target) + '.tmp')
        stale.write_text('unchanged')
        os.chmod(stale, 0o644)
        gw._save_config_locked(target, {'host': '127.0.0.1', 'port': 18992, 'devices': {}})
        self.assertEqual(stale.read_text(), 'unchanged')
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_health_requires_token(self):
        self.assertEqual(self.req('GET', '/v1/health')[0], 401)
        self.assertEqual(self.req('GET', '/v1/health', 'x' * 40)[0], 401)
        self.assertEqual(self.req('GET', '/v1/health', self.ro_token)[0], 200)

    def test_recall_ok_for_read_scope_and_actor_is_device(self):
        st, out = self.req('POST', '/v1/memory_recall', self.ro_token, {'query': 'q', 'project_tag': 'project:fixture'})
        self.assertEqual(st, 200)
        self.assertTrue(out['ok'])
        self.assertEqual(out['result']['actor'], 'win-ro')

    def test_store_forbidden_for_read_only_device(self):
        st, out = self.req('POST', '/v1/memory_store', self.ro_token,
                           {'content': 'c', 'request_id': 'r-12345678', 'project_tag': 'project:fixture'})
        self.assertEqual((st, out['error']), (403, 'forbidden_scope'))

    def test_store_ok_and_client_cannot_choose_actor(self):
        n = len(self.backend.calls)
        st, _ = self.req('POST', '/v1/memory_store', self.rw_token,
                         {'content': 'c', 'request_id': 'r-12345678', 'project_tag': 'project:fixture', 'actor': 'fixture-owner'})
        self.assertEqual(st, 400)  # unknown argument rejected
        self.assertEqual(len(self.backend.calls), n)
        st, out = self.req('POST', '/v1/memory_store', self.rw_token,
                           {'content': 'c', 'request_id': 'r-12345678', 'project_tag': 'project:fixture'})
        self.assertEqual(st, 200)
        self.assertEqual(out['result']['actor'], 'win-rw')

    def test_unknown_path_bad_json_oversize(self):
        self.assertEqual(self.req('POST', '/v1/memory_write', self.rw_token, {})[0], 404)
        self.assertEqual(self.req('POST', '/v1/memory_recall', self.rw_token, raw=b'{nope')[0], 400)
        self.assertEqual(self.req('POST', '/v1/memory_recall', self.rw_token, raw=b'[1]')[0], 400)
        import http.client  # declare an oversize body without sending it: the server must refuse from the header
        conn = http.client.HTTPSConnection('127.0.0.1', 18991, context=self.ctx, timeout=10)
        conn.putrequest('POST', '/v1/memory_recall')
        conn.putheader('Authorization', f'Bearer {self.rw_token}')
        conn.putheader('Content-Length', str(gw.MAX_BODY_BYTES + 10))
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 413)
        conn.close()

    def test_ambiguous_http_framing_is_rejected(self):
        import http.client
        for headers in ((('Content-Length', '2'), ('Content-Length', '2')),
                        (('Content-Length', '2'), ('Transfer-Encoding', 'chunked'))):
            conn = http.client.HTTPSConnection('127.0.0.1', 18991, context=self.ctx, timeout=10)
            conn.putrequest('POST', '/v1/memory_recall')
            conn.putheader('Authorization', f'Bearer {self.rw_token}')
            for name, value in headers:
                conn.putheader(name, value)
            conn.endheaders()
            self.assertEqual(conn.getresponse().status, 400)
            conn.close()

    def test_excessively_long_content_length_is_rejected(self):
        import http.client
        conn = http.client.HTTPSConnection('127.0.0.1', 18991, context=self.ctx, timeout=10)
        conn.putrequest('POST', '/v1/memory_recall')
        conn.putheader('Authorization', f'Bearer {self.rw_token}')
        conn.putheader('Content-Length', '9' * 5000)
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 413)
        conn.close()

    def test_stalled_tls_handshake_does_not_block_another_client(self):
        import socket
        stalled = socket.create_connection(('127.0.0.1', 18991), timeout=3)
        try:
            self.assertEqual(self.req('GET', '/v1/health', self.ro_token)[0], 200)
        finally:
            stalled.close()

    def test_stalled_unauthenticated_connections_expire(self):
        import socket
        import time
        sockets = []
        with patch.object(gw, 'AUTH_DEADLINE', 0.2):
            try:
                for _ in range(gw.MAX_CONCURRENT):
                    sockets.append(socket.create_connection(('127.0.0.1', 18991), timeout=3))
                time.sleep(0.8)
                self.assertEqual(self.req('GET', '/v1/health', self.ro_token)[0], 200)
            finally:
                for sock in sockets:
                    sock.close()

    def test_authenticated_incomplete_body_has_absolute_deadline(self):
        import http.client
        import time
        conn = http.client.HTTPSConnection('127.0.0.1', 18991, context=self.ctx, timeout=3)
        with patch.object(gw, 'BODY_DEADLINE', 0.3):
            try:
                conn.putrequest('POST', '/v1/memory_recall')
                conn.putheader('Authorization', f'Bearer {self.ro_token}')
                conn.putheader('Content-Length', '100')
                conn.endheaders()
                conn.send(b'{')
                time.sleep(0.8)
                with self.assertRaises((OSError, http.client.HTTPException)):
                    conn.getresponse()
                self.assertEqual(self.req('GET', '/v1/health', self.ro_token)[0], 200)
            finally:
                conn.close()

    def test_errors_never_leak_details(self):
        st, out = self.req('POST', '/v1/memory_recall', self.rw_token, {'query': 'boom', 'project_tag': 'p'})
        self.assertEqual((st, out['error']), (500, 'internal_error'))
        self.assertNotIn('secret', json.dumps(out))
        st, out = self.req('POST', '/v1/memory_recall', self.rw_token, {'query': 'bad', 'project_tag': 'p'})
        self.assertEqual((st, out['error']), (400, 'invalid_query'))

    def test_plain_http_and_untrusted_ca_rejected(self):
        with self.assertRaises(Exception):
            urllib.request.urlopen('http://127.0.0.1:18991/v1/health', timeout=5)
        with self.assertRaises(urllib.error.URLError) as caught:
            urllib.request.urlopen(self.base + '/v1/health', timeout=5, context=ssl.create_default_context())
        self.assertIsInstance(caught.exception.reason, ssl.SSLError)

    def test_config_rejects_public_or_wildcard_bind(self):
        d = Path(self.tmp.name)
        for host in ('0.0.0.0', '8.8.8.8', '::1', 'example.com'):
            p = d / 'bad.json'
            gw._save_config_locked(p, {'host': host, 'port': 18992, 'devices': {}})
            with self.assertRaises(gw.GatewayConfigError):
                gw.load_config(p, require_files=False)

    def test_relay_gateway_journal_and_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            os.chmod(root, 0o700)
            lock = root / 'target.lock'
            lock.touch(mode=0o600)
            target = FakeTarget()
            previous = self.server.backend
            self.server.backend = HindsightBackend(target, root, lock, ['project:fixture'])
            try:
                relay = Relay(self.base, self.ca, hashlib.sha256(Path(self.ca).read_bytes()).hexdigest())
                args = {'request_id': 'fixture-abc', 'content': 'fictional', 'project_tag': 'project:fixture'}
                with patch.dict(os.environ, {'SIOS_MEMORY_GATEWAY_TOKEN': self.rw_token}):
                    error, result = relay.forward('memory_store', args)
                    self.assertFalse(error)
                    self.assertEqual(result['status'], 'committed_exact_readback')
                    error, read = relay.forward('memory_read_primary', {'path': result['source_path']})
                    self.assertFalse(error)
                    self.assertEqual(read['text'], 'fictional')
                    self.assertEqual(read['source_revision'], result['source_revision'])
                    target.lose_reply = True
                    error, response = relay.forward('memory_store', {**args, 'request_id': 'fixture-def'})
                    self.assertTrue(error)
                    self.assertEqual(response['error'], 'unknown_no_retry')
                    self.assertEqual(target.sends, 2)
                    error, reconciled = relay.forward('memory_store_reconcile', {'request_id': 'fixture-def'})
                    self.assertFalse(error)
                    self.assertEqual(reconciled['status'], 'reconciled_committed')
                    self.assertEqual(target.sends, 2)
            finally:
                self.server.backend = previous



if __name__ == "__main__":
    unittest.main()
