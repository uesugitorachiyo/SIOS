"""Authorization boundary for selective satellite Memory capture."""

import os
import http.client
import json
import threading
import unittest
from unittest.mock import patch

from aios.server import Device, device_from_env, make_server
from aios.service import dispatch


class MemoryScopeTests(unittest.TestCase):
    def test_server_accepts_narrow_memory_scopes(self):
        with patch.dict(os.environ, {'SCOPE_TEST_TOKEN': 'x' * 32}):
            device = device_from_env('scope-test', 'SCOPE_TEST_TOKEN',
                                     {'read', 'memory:capture', 'memory:index_refresh'})
        self.assertEqual(device.scopes,
                         frozenset({'read', 'memory:capture', 'memory:index_refresh'}))

    def test_narrow_client_can_capture_and_refresh_without_raw_write(self):
        scopes = {'read', 'memory:capture', 'memory:index_refresh'}
        store = object()
        with patch('aios.selective_memory.capture_with_store', return_value={'retained': False}) as capture:
            result = dispatch(store, 'memory_capture', {'request_json': '{"retain":false}'},
                              actor='satellite', scopes=scopes)
        self.assertEqual(result, {'retained': False})
        self.assertEqual(capture.call_args.kwargs['actor'], 'satellite')
        with patch('aios.indexed_note.refresh', return_value={'status': 'synthetic'}) as refresh:
            result = dispatch(store, 'memory_index_refresh',
                              {'identifier': 'sample', 'expected_revision': 'a' * 64,
                               'query': 'sample-marker'}, actor='satellite', scopes=scopes)
        self.assertEqual(result, {'status': 'synthetic'})
        self.assertIs(refresh.call_args.args[0], store)
        self.assertNotIn('actor', refresh.call_args.kwargs)
        for operation in ('memory_write', 'publish', 'mission_write', 'brain_propose'):
            with self.subTest(operation=operation), self.assertRaises(PermissionError):
                params = ({'identifier': 'sample', 'content': 'synthetic',
                           'request_id': 'sample-request'} if operation == 'memory_write' else
                          {'routine': 'morning-brief', 'run_id': 'sample-run',
                           'report': 'synthetic', 'summary': 'synthetic',
                           'status': 'draft', 'request_id': 'sample-request'} if operation == 'publish' else {})
                dispatch(store, operation, params, actor='satellite', scopes=scopes)

    def test_narrow_scopes_do_not_unlock_each_other(self):
        with self.assertRaises(PermissionError):
            dispatch(object(), 'memory_index_refresh', {}, actor='satellite',
                     scopes={'read', 'memory:capture'})
        with self.assertRaises(PermissionError):
            dispatch(object(), 'memory_capture', {'request_json': '{"retain":false}'},
                     actor='satellite', scopes={'read', 'memory:index_refresh'})

    def test_read_only_cannot_capture_or_refresh(self):
        for operation in ('memory_capture', 'memory_index_refresh'):
            with self.subTest(operation=operation), self.assertRaises(PermissionError):
                dispatch(object(), operation, {}, actor='reader', scopes={'read'})

    def test_existing_write_scope_keeps_capture_and_refresh(self):
        scopes = {'read', 'memory:write'}
        with patch('aios.selective_memory.capture_with_store', return_value={'retained': False}):
            self.assertEqual(dispatch(object(), 'memory_capture',
                                      {'request_json': '{"retain":false}'},
                                      actor='existing', scopes=scopes), {'retained': False})
        with patch('aios.indexed_note.refresh', return_value={'status': 'synthetic'}):
            self.assertEqual(dispatch(object(), 'memory_index_refresh',
                                      {'identifier': 'sample', 'expected_revision': 'a' * 64,
                                       'query': 'sample-marker'}, actor='existing', scopes=scopes),
                             {'status': 'synthetic'})
        class ExistingStore:
            def write_memory(self, **params):
                return params

            def publish(self, **params):
                return params

        store = ExistingStore()
        write = dispatch(store, 'memory_write',
                         {'identifier': 'sample', 'content': 'synthetic',
                          'request_id': 'sample-request'}, actor='existing', scopes=scopes)
        self.assertEqual(write['actor'], 'existing')
        publish = dispatch(store, 'publish',
                           {'routine': 'morning-brief', 'run_id': 'sample-run',
                            'report': 'synthetic', 'summary': 'synthetic',
                            'status': 'draft', 'request_id': 'sample-request'},
                           actor='existing', scopes=scopes)
        self.assertEqual(publish['actor'], 'existing')

    def test_narrow_write_still_requires_read_for_dedup_and_return(self):
        for operation, narrow in (('memory_capture', 'memory:capture'),
                                  ('memory_index_refresh', 'memory:index_refresh')):
            with self.subTest(operation=operation), self.assertRaises(PermissionError):
                dispatch(object(), operation, {}, actor='satellite', scopes={narrow})

    def test_http_surface_enforces_narrow_scope(self):
        instance = '00000000-0000-4000-8000-000000000001'
        token = 'n' * 32
        devices = [
            Device('satellite', token,
                   frozenset({'read', 'memory:capture', 'memory:index_refresh'})),
            Device('capture-only', 'c' * 32, frozenset({'read', 'memory:capture'})),
            Device('refresh-only', 'r' * 32, frozenset({'read', 'memory:index_refresh'})),
        ]
        server = make_server(object(), instance, devices, bind='127.0.0.1', port=0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def call(operation, params, *, auth_token=token):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                body = json.dumps({'instance_id': instance, 'operation': operation,
                                   'params': params}).encode('utf-8')
                conn.request('POST', '/v1/rpc', body=body,
                             headers={'Authorization': 'Bearer ' + auth_token,
                                      'Content-Type': 'application/json'})
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()

        try:
            status, body = call('memory_capture', {'request_json': '{"retain":false}'})
            self.assertEqual(status, 200)
            self.assertEqual(body['result'],
                             {'status': 'skipped', 'reason': 'explicit_nonretention'})
            with patch('aios.indexed_note.refresh', return_value={'status': 'synthetic'}) as refresh:
                status, body = call('memory_index_refresh',
                                    {'identifier': 'sample', 'expected_revision': 'a' * 64,
                                     'query': 'sample-marker'})
            self.assertEqual(status, 200)
            self.assertEqual(body['result'], {'status': 'synthetic'})
            self.assertEqual(refresh.call_args.kwargs,
                             {'identifier': 'sample', 'expected_revision': 'a' * 64,
                              'query': 'sample-marker'})
            status, body = call('memory_index_refresh', {}, auth_token='c' * 32)
            self.assertEqual((status, body['error']), (403, 'forbidden'))
            status, body = call('memory_capture', {'request_json': '{"retain":false}'},
                                auth_token='r' * 32)
            self.assertEqual((status, body['error']), (403, 'forbidden'))
            for operation, params in (
                ('memory_write', {'identifier': 'sample', 'content': 'synthetic',
                                  'request_id': 'sample-request'}),
                ('publish', {'routine': 'morning-brief', 'run_id': 'sample-run',
                             'report': 'synthetic', 'summary': 'synthetic',
                             'status': 'draft', 'request_id': 'sample-request'}),
            ):
                with self.subTest(operation=operation):
                    status, body = call(operation, params)
                    self.assertEqual((status, body['error']), (403, 'forbidden'))
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
