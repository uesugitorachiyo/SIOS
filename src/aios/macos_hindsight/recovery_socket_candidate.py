"""Single-listener recovery candidate for a dedicated Unix service account.

The supervisor prepares a private directory and an approved request before launch.
Only the gate listens. The Hindsight application and its API key stay in process.
"""
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys

MAX_BODY = 600 * 1024


def canonical(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate object key')
            result[key] = value
        return result

    obj = json.loads(data.decode('utf-8'), object_pairs_hook=unique)
    return obj, json.dumps(obj, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _private_value(path, owner, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise PermissionError('unsafe recovery service file')
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError('recovery service file exceeds its size limit')
    return data


def read_approval(root, owner=None):
    return _private_value(root / 'approved.json', os.getuid() if owner is None else owner, MAX_BODY)


def read_gate_key(root, owner=None):
    token = _private_value(root / 'gate.key', os.getuid() if owner is None else owner, 128)
    if not re.fullmatch(rb'[A-Za-z0-9_-]{43}', token):
        raise ValueError('invalid recovery service key file')
    return token.decode('ascii')


class RecoverySocket:
    def __init__(self, app, root, approved, client_key, internal_key):
        obj, self.approved = canonical(approved)
        if set(obj) != {'async', 'items'} or obj['async'] is not False or len(obj['items']) != 1:
            raise ValueError('expected a single synchronous recovery request')
        document = obj['items'][0]['document_id']
        if not re.fullmatch(r'primary-[0-9a-f]{64}', document):
            raise ValueError('invalid recovery document ID')
        self.app = app
        self.client_auth = b'Bearer ' + client_key.encode('ascii')
        self.internal_auth = b'Bearer ' + internal_key.encode('ascii')
        self.root = root
        self.receipt = f'{document}.spent'
        bank = '/v1/default/banks/sios-memory'
        self.write = ('POST', f'{bank}/memories')
        self.reads = {('GET', f'{bank}/config'), ('GET', f'{bank}/documents/{document}')}

    def reserve(self):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory = os.open(self.root, flags)
        try:
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise PermissionError('unsafe recovery state directory')
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            with os.fdopen(os.open(self.receipt, flags, 0o600, dir_fd=directory), 'wb') as out:
                out.write(b'spent\n')
                out.flush()
                os.fsync(out.fileno())
            os.fsync(directory)
        finally:
            os.close(directory)

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            await self.app(scope, receive, send)
            return
        if scope['type'] == 'websocket':
            await send({'type': 'websocket.close', 'code': 1008})
            return

        async def reject(status):
            await send({'type': 'http.response.start', 'status': status, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})

        headers = scope['headers']
        auth = [value for name, value in headers if name.lower() == b'authorization']
        if len(auth) != 1 or not hmac.compare_digest(auth[0], self.client_auth):
            return await reject(401)
        route = (scope['method'], scope['path'])
        if scope.get('query_string') or route not in self.reads | {self.write}:
            return await reject(403)
        body = bytearray()
        if route == self.write:
            while True:
                msg = await receive()
                if msg['type'] != 'http.request':
                    return
                body.extend(msg.get('body', b''))
                if len(body) > MAX_BODY:
                    return await reject(413)
                if not msg.get('more_body'):
                    break
            try:
                _, approved = canonical(body)
            except (ValueError, RecursionError):
                return await reject(400)
            if not hmac.compare_digest(approved, self.approved):
                return await reject(403)
            try:
                self.reserve()
            except FileExistsError:
                return await reject(409)
            except OSError:
                return await reject(503)
            body = self.approved

        delivered = False

        async def controlled_receive():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {'type': 'http.request', 'body': body, 'more_body': False}

        forwarded = dict(scope, headers=[(b'authorization', self.internal_auth),
                                         (b'content-type', b'application/json'),
                                         (b'content-length', str(len(body)).encode())])
        await self.app(forwarded, controlled_receive, send)


if __name__ == '__main__':
    os.umask(0o077)
    root = Path(sys.argv[1])
    info = root.lstat()
    if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise SystemExit('unsafe recovery directory')
    if sys.argv[2:] == ['--private-key-file']:
        key = read_gate_key(root)
    elif len(sys.argv) == 2:
        key = os.environ.pop('SIOS_RECOVERY_GATE_KEY')  # disposable fixture launchers
    else:
        raise SystemExit('invalid recovery launcher arguments')
    internal_key = secrets.token_urlsafe(32)
    os.environ['HINDSIGHT_API_TENANT_API_KEY'] = internal_key
    os.environ['HINDSIGHT_API_MCP_ENABLED'] = 'false'
    os.environ['HINDSIGHT_API_WORKER_ENABLED'] = 'false'
    os.environ['HINDSIGHT_API_RUN_MIGRATIONS_ON_STARTUP'] = 'false'
    approved = read_approval(root)
    from hindsight_api.server import app
    import socket
    import uvicorn

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(root / 'api.sock'))
        listener.listen()
        gate = RecoverySocket(app, root, approved, key, internal_key)
        uvicorn.Server(uvicorn.Config(gate, access_log=False, proxy_headers=False, ws='none')).run(sockets=[listener])
