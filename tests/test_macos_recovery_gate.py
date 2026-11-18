import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from aios.macos_hindsight.recovery_socket_candidate import RecoverySocket


class GateTests(unittest.TestCase):
    def test_only_one_exact_approved_write_reaches_application(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = 'primary-' + 'a' * 64
            approved = {'async': False, 'items': [{'document_id': document, 'content': 'fixture'}]}
            sent = []
            async def application(scope, receive, send):
                data = await receive()
                sent.append((scope['headers'], data['body']))
                await send({'type': 'http.response.start', 'status': 200, 'headers': []})
                await send({'type': 'http.response.body', 'body': b'{}'})
            gate = RecoverySocket(application, root, json.dumps(approved).encode(), 'fixture-key', 'backend-key')
            async def request(body, authorization=b'Bearer fixture-key', path='/v1/default/banks/sios-memory/memories'):
                emitted = []
                async def receive():
                    return {'type': 'http.request', 'body': body, 'more_body': False}
                async def send(value):
                    emitted.append(value)
                await gate({'type': 'http', 'method': 'POST', 'path': path, 'query_string': b'',
                            'headers': [(b'authorization', authorization)]}, receive, send)
                return emitted[0]['status']
            payload = json.dumps(approved).encode()
            self.assertEqual(asyncio.run(request(payload, b'Bearer wrong')), 401)
            self.assertEqual(asyncio.run(request(payload, path='/v1/default/banks/sios-memory/other')), 403)
            changed = dict(approved, items=[{'document_id': document, 'content': 'different'}])
            self.assertEqual(asyncio.run(request(json.dumps(changed).encode())), 403)
            self.assertEqual(asyncio.run(request(payload)), 200)
            self.assertEqual(asyncio.run(request(payload)), 409)
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0][0][0], (b'authorization', b'Bearer backend-key'))
            self.assertEqual(json.loads(sent[0][1]), approved)


if __name__ == '__main__':
    unittest.main()
