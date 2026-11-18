"""Hindsight transport for the local recovery service's Unix socket."""
import http.client
import json
from pathlib import Path
import re
import socket
from urllib.error import HTTPError


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout):
        super().__init__('localhost', timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(self.timeout)
            connection.connect(str(self.socket_path))
        except BaseException:
            connection.close()
            raise
        self.sock = connection


class UnixRecoveryTarget:
    def __init__(self, socket_path, journal_root, token, *, write_timeout=30, lock_wait=10):
        self.socket_path = Path(socket_path)
        if not self.socket_path.is_absolute():
            raise ValueError('recovery socket requires an absolute path')
        self.endpoint = 'unix:' + str(self.socket_path)
        self.journal_root = Path(journal_root)
        self.write_timeout = write_timeout
        self.lock_wait = lock_wait
        self._token = token

    def client(self, timeout):
        return _Client(self.socket_path, self._token, timeout)


class _Client:
    def __init__(self, socket_path, token, timeout):
        self.socket_path = socket_path
        self.token = token
        self.timeout = timeout

    def _request(self, path, payload=None):
        if path not in ('/config', '/memories') and not re.fullmatch(r'/documents/primary-[0-9a-f]{64}', path):
            raise ValueError('invalid recovery path')
        connection = _UnixHTTPConnection(self.socket_path, self.timeout)
        try:
            request = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            connection.request('GET' if request is None else 'POST', '/v1/default/banks/sios-memory' + path,
                               body=request, headers={'Authorization': 'Bearer ' + self.token,
                                                      'Content-Type': 'application/json'})
            with connection.getresponse() as response:
                if response.status != 200:
                    raise HTTPError(path, response.status, 'recovery endpoint refused', None, None)
                data = response.read(8 * 1024 * 1024 + 1)
        finally:
            connection.close()
        if len(data) > 8 * 1024 * 1024:
            raise ValueError('recovery response too large')
        result = json.loads(data)
        if not isinstance(result, dict) or result.get('error') is not None:
            raise ValueError('unconfirmed recovery response')
        return result

    def get(self, document_id):
        if not re.fullmatch(r'primary-[0-9a-f]{64}', document_id):
            raise ValueError('invalid document ID')
        try:
            value = self._request('/documents/' + document_id)
        except HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        return {'text': value.get('original_text'), 'metadata': value.get('document_metadata')}
