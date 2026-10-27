"""Verify the local browser boundary and read-only integration."""
import http.client
import hashlib
import json
import threading
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aios.evidence_search_web import create_server, mcp_value, registered_launcher


class EvidenceWebTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        def call(op, args):
            self.calls.append((op, args))
            if op == 'memory_index_search':
                return {'results': [{'read_path': 'Memory/notes/one.md'}], 'has_more': False}
            if op == 'search':
                return []
            if op == 'read':
                text = '# Source\n<script>unsafe()</script>'
                return {'path': args['relative'], 'revision': hashlib.sha256(text.encode()).hexdigest(),
                        'text': text}
            raise AssertionError(op)
        self.server = create_server(call)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        status, _, html = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.html = html.decode()
        self.csrf = self.server.access_url.split('#', 1)[1]
        self.assertNotIn(self.csrf, self.html)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def headers(self):
        return {'Origin': 'http://127.0.0.1:' + str(self.port),
                'Content-Type': 'application/json', 'X-SIOS-CSRF': self.csrf}

    def test_valid_search_and_safe_rendering(self):
        status, headers, body = self.request('POST', '/search', json.dumps({'query': 'one'}), self.headers())
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result['results'][0]['text'], '# Source\n<script>unsafe()</script>')
        self.assertIn({'store':'memory','reason':'index_may_lag'}, result['warnings'])
        self.assertIn('no-store', headers['Cache-Control'])
        self.assertIn('textContent', self.html)
        self.assertNotIn('innerHTML', self.html)
        self.assertTrue(all(op in ('search','memory_index_search','read') for op, _ in self.calls))

    def test_host_origin_and_csrf_are_enforced(self):
        body = json.dumps({'query': 'x'})
        for override in ({'Host': 'evil.invalid'}, {'Origin': 'https://evil.invalid'}, {'X-SIOS-CSRF': 'wrong'}):
            headers = self.headers(); headers.update(override)
            status, _, _ = self.request('POST', '/search', body, headers)
            self.assertEqual(status, 403)
        status, _, _ = self.request('GET', '/', headers={'Host':'evil.invalid'})
        self.assertEqual(status, 404)
        self.assertEqual(self.calls, [])

    def test_rejects_unexpected_data_and_paths(self):
        status, _, _ = self.request('POST', '/search', json.dumps({'query':'x','path':'Memory/notes/other.md'}), self.headers())
        self.assertEqual(status, 400)
        self.assertEqual(self.request('GET','/secret')[0],404)
        self.assertEqual(self.calls, [])

    def test_scope_and_failure_do_not_return_private_exception_text(self):
        def failed(op, args):
            raise RuntimeError('private secret or path')
        other = create_server(failed)
        t = threading.Thread(target=other.serve_forever, daemon=True); t.start()
        try:
            old = self.server, self.port, self.csrf
            self.server, self.port = other, other.server_address[1]
            self.csrf = other.access_url.split('#', 1)[1]
            status, _, body = self.request('POST','/search',json.dumps({'query':'x'}),self.headers())
            self.assertEqual(status,200)
            self.assertNotIn(b'private secret or path',body)
            self.assertEqual(len(json.loads(body)['errors']),3)
            self.server, self.port, self.csrf = old
        finally:
            other.shutdown();other.server_close();t.join()

    def test_native_mcp_list_and_error_formats(self):
        block = lambda text: SimpleNamespace(type='text', text=text)
        result = SimpleNamespace(is_error=False, structured_content=None,
                                 content=[block('{"path":"one"}'),block('{"path":"two"}')])
        self.assertEqual(mcp_value(result,'search'), [{'path':'one'},{'path':'two'}])
        result.content=[]
        self.assertEqual(mcp_value(result,'search'),[])
        result.is_error=True
        with self.assertRaises(RuntimeError):mcp_value(result,'search')

    def test_registered_client_config_is_explicit_and_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'config.toml'
            path.write_text('[mcp_servers.sios-l0]\ncommand = "python"\nargs = ["launcher.py"]\ncwd = "work"\n')
            self.assertEqual(registered_launcher('sios-l0',path),('python',['launcher.py'],{},'work'))
            with self.assertRaises(ValueError):registered_launcher('unrelated',path)
            with self.assertRaises(ValueError):registered_launcher('sios-nucx',path)

    def test_source_bytes_are_bounded(self):
        text = 'x' * 2_100_000
        reads=[]
        def call(op, args):
            if op == 'memory_index_search':
                return {'results': [{'read_path':f'Memory/notes/{n}.md'} for n in (1,2,3)], 'has_more':False}
            if op == 'search': return []
            if op == 'read':
                reads.append(args['relative'])
                return {'path':args['relative'],'text':text,'revision':hashlib.sha256(text.encode()).hexdigest()}
        other=create_server(call);t=threading.Thread(target=other.serve_forever,daemon=True);t.start()
        try:
            old=self.server,self.port,self.csrf
            self.server,self.port,self.csrf=other,other.server_address[1],other.access_url.split('#',1)[1]
            with patch('aios.evidence_search_web.MAX_SOURCE_BYTES', 4_000_000):
                status,_,body=self.request('POST','/search',json.dumps({'query':'x'}),self.headers())
            result=json.loads(body);self.assertEqual(status,200);self.assertEqual(len(result['results']),1)
            self.assertTrue(result['truncated']);self.assertIn({'store':'all','reason':'response_limit'},result['warnings'])
            self.assertEqual(len(reads),2)
            self.server,self.port,self.csrf=old
        finally:
            other.shutdown();other.server_close();t.join()

    def test_only_one_search_can_run_at_a_time(self):
        entered=threading.Event();release=threading.Event()
        def call(op,args):
            if op=='search':entered.set();release.wait(3);return []
            if op=='memory_index_search':return {'results':[],'has_more':False}
            raise AssertionError(op)
        other=create_server(call);t=threading.Thread(target=other.serve_forever,daemon=True);t.start()
        try:
            old=self.server,self.port,self.csrf
            self.server,self.port,self.csrf=other,other.server_address[1],other.access_url.split('#',1)[1]
            first=[]
            worker=threading.Thread(target=lambda:first.append(self.request('POST','/search',json.dumps({'query':'x'}),self.headers())))
            worker.start();self.assertTrue(entered.wait(3))
            status,_,_=self.request('POST','/search',json.dumps({'query':'y'}),self.headers())
            self.assertEqual(status,429);release.set();worker.join(5);self.assertEqual(first[0][0],200)
            self.server,self.port,self.csrf=old
        finally:
            release.set();other.shutdown();other.server_close();t.join()

    def test_encoded_response_limit_is_reported(self):
        with patch('aios.evidence_search_web.MAX_RESPONSE_BYTES', 350):
            status,_,body=self.request('POST','/search',json.dumps({'query':'one'}),self.headers())
        result=json.loads(body);self.assertEqual(status,200);self.assertLessEqual(len(body),350)
        self.assertTrue(result['truncated'])
        self.assertIn({'store':'all','reason':'response_limit'},result['warnings'])

    def test_time_limit_marks_partial_results(self):
        def call(op,args):
            time.sleep(.03)
            return [] if op=='search' else {'results':[],'has_more':False}
        other=create_server(call);t=threading.Thread(target=other.serve_forever,daemon=True);t.start()
        try:
            old=self.server,self.port,self.csrf
            self.server,self.port,self.csrf=other,other.server_address[1],other.access_url.split('#',1)[1]
            with patch('aios.evidence_search_web.SEARCH_SECONDS',.01):
                status,_,body=self.request('POST','/search',json.dumps({'query':'x'}),self.headers())
            result=json.loads(body);self.assertEqual(status,200);self.assertTrue(result['truncated'])
            self.assertIn({'store':'all','reason':'time_limit'},result['warnings'])
            self.server,self.port,self.csrf=old
        finally:
            other.shutdown();other.server_close();t.join()

if __name__ == '__main__':
    unittest.main()
