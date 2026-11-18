"""Minimal dependency-free PostgreSQL client (simple-query protocol only).

Used by ``hindsight_pg_writer_fence_candidate`` so the fence needs no
third-party driver. Every ``PgConnection`` is a separate server backend, which
is what the multi-connection fence tests rely on.

Scope is deliberately small: protocol 3.0, Unix-socket or TCP transport, TLS
for TCP (caller-supplied ``ssl.SSLContext``), trust / SCRAM-SHA-256 / MD5
authentication (cleartext passwords only over a Unix socket), simple queries
with text results, ``COPY FROM STDIN`` and SQLSTATE-bearing errors. There are
no bind parameters: callers must build SQL only from ``ident``/``literal``
quoted values. ``literal`` refuses to run unless the server reports
``standard_conforming_strings=on``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import socket
import ssl
import struct

PROTOCOL_3 = 196608
SSL_REQUEST = 80877103
_IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,62}\Z')


class PgError(Exception):
    """Server ErrorResponse; ``sqlstate`` and ``message`` are server fields."""

    def __init__(self, fields):
        self.fields = dict(fields)
        self.sqlstate = self.fields.get('C', '')
        self.message = self.fields.get('M', '')
        super().__init__(f'{self.sqlstate}: {self.message}')


class PgProtocolError(RuntimeError):
    pass


def ident(name):
    """Quote a plain identifier; anything outside [A-Za-z0-9_] is refused."""
    if not isinstance(name, str) or not _IDENT.fullmatch(name):
        raise ValueError('invalid_identifier')
    return '"' + name + '"'


def _scram_hi(password, salt, iterations):
    return hashlib.pbkdf2_hmac('sha256', password, salt, iterations)


class PgConnection:
    def __init__(self, *, user, database, host=None, port=5432, unix_dir=None,
                 password=None, application_name='hindsight_writer_fence',
                 timeout=30.0, ssl_context=None, options=None):
        if (unix_dir is None) == (host is None):
            raise ValueError('exactly_one_of_unix_dir_or_host')
        self._sock = None
        self.parameters = {}
        self.backend_pid = None
        self.status = None
        if unix_dir is not None:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(os.path.join(unix_dir, f'.s.PGSQL.{int(port)}'))
            secure = True
        else:
            sock = socket.create_connection((host, int(port)), timeout=timeout)
            secure = False
            if ssl_context is not None:
                sock.sendall(struct.pack('!II', 8, SSL_REQUEST))
                if sock.recv(1) != b'S':
                    sock.close()
                    raise PgProtocolError('server_refused_tls')
                sock = ssl_context.wrap_socket(sock, server_hostname=host)
                secure = True
        self._sock = sock
        self._buf = b''
        try:
            params = {'user': user, 'database': database, 'client_encoding': 'UTF8',
                      'application_name': application_name}
            if options:
                params['options'] = options
            body = struct.pack('!I', PROTOCOL_3) + b''.join(
                k.encode() + b'\0' + str(v).encode() + b'\0' for k, v in params.items()) + b'\0'
            self._sock.sendall(struct.pack('!I', len(body) + 4) + body)
            self._authenticate(user, password, secure)
            self._drain_until_ready()
        except BaseException:
            self.close()
            raise

    # -- low level -------------------------------------------------------
    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise PgProtocolError('connection_closed')
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def _read(self):
        kind, length = struct.unpack('!cI', self._recv_exact(5))
        return kind, self._recv_exact(length - 4)

    def _send(self, kind, payload=b''):
        self._sock.sendall(kind + struct.pack('!I', len(payload) + 4) + payload)

    @staticmethod
    def _fields(payload):
        fields = {}
        for part in payload.split(b'\0'):
            if part:
                fields[chr(part[0])] = part[1:].decode('utf-8', 'replace')
        return fields

    # -- authentication --------------------------------------------------
    def _authenticate(self, user, password, secure):
        while True:
            kind, payload = self._read()
            if kind == b'E':
                raise PgError(self._fields(payload))
            if kind != b'R':
                raise PgProtocolError('unexpected_auth_message')
            code = struct.unpack('!I', payload[:4])[0]
            if code == 0:
                return
            if password is None:
                raise PgProtocolError('password_required')
            if code == 3:
                if not secure:
                    raise PgProtocolError('cleartext_password_refused')
                self._send(b'p', password.encode() + b'\0')
            elif code == 5:
                salt = payload[4:8]
                inner = hashlib.md5(password.encode() + user.encode()).hexdigest().encode()
                self._send(b'p', b'md5' + hashlib.md5(inner + salt).hexdigest().encode() + b'\0')
            elif code == 10:
                mechanisms = [m for m in payload[4:].split(b'\0') if m]
                if b'SCRAM-SHA-256' not in mechanisms:
                    raise PgProtocolError('no_supported_sasl_mechanism')
                self._scram(password)
                return
            else:
                raise PgProtocolError('unsupported_authentication')

    def _scram(self, password):
        nonce = base64.b64encode(os.urandom(18)).decode()
        first_bare = f'n=,r={nonce}'
        first = ('n,,' + first_bare).encode()
        self._send(b'p', b'SCRAM-SHA-256\0' + struct.pack('!I', len(first)) + first)
        kind, payload = self._read()
        if kind == b'E':
            raise PgError(self._fields(payload))
        if kind != b'R' or struct.unpack('!I', payload[:4])[0] != 11:
            raise PgProtocolError('scram_continue_expected')
        server_first = payload[4:].decode()
        attrs = dict(item.split('=', 1) for item in server_first.split(','))
        if not attrs['r'].startswith(nonce):
            raise PgProtocolError('scram_nonce_mismatch')
        salted = _scram_hi(password.encode(), base64.b64decode(attrs['s']), int(attrs['i']))
        client_key = hmac.new(salted, b'Client Key', hashlib.sha256).digest()
        stored_key = hashlib.sha256(client_key).digest()
        without_proof = f'c=biws,r={attrs["r"]}'
        auth_message = f'{first_bare},{server_first},{without_proof}'.encode()
        signature = hmac.new(stored_key, auth_message, hashlib.sha256).digest()
        proof = base64.b64encode(bytes(a ^ b for a, b in zip(client_key, signature))).decode()
        self._send(b'p', f'{without_proof},p={proof}'.encode())
        kind, payload = self._read()
        if kind == b'E':
            raise PgError(self._fields(payload))
        if kind != b'R' or struct.unpack('!I', payload[:4])[0] != 12:
            raise PgProtocolError('scram_final_expected')
        server_key = hmac.new(salted, b'Server Key', hashlib.sha256).digest()
        expected = base64.b64encode(hmac.new(server_key, auth_message, hashlib.sha256).digest()).decode()
        if not hmac.compare_digest(payload[4:].decode(), 'v=' + expected):
            raise PgProtocolError('scram_server_signature_mismatch')
        kind, payload = self._read()
        if kind != b'R' or struct.unpack('!I', payload[:4])[0] != 0:
            raise PgProtocolError('scram_auth_not_completed')

    def _drain_until_ready(self):
        error = None
        while True:
            kind, payload = self._read()
            if kind == b'S':
                key, value = payload.rstrip(b'\0').split(b'\0', 1)
                self.parameters[key.decode()] = value.decode()
            elif kind == b'K':
                self.backend_pid = struct.unpack('!I', payload[:4])[0]
            elif kind == b'E':
                error = PgError(self._fields(payload))
            elif kind == b'Z':
                self.status = payload[:1].decode()
                if error is not None:
                    raise error
                return

    # -- public ----------------------------------------------------------
    def literal(self, value):
        if self.parameters.get('standard_conforming_strings') != 'on':
            raise PgProtocolError('standard_conforming_strings_required')
        if value is None:
            return 'NULL'
        if isinstance(value, bool):
            return 'true' if value else 'false'
        if isinstance(value, int):
            return str(value)
        if not isinstance(value, str) or '\0' in value:
            raise ValueError('invalid_literal')
        return "'" + value.replace("'", "''") + "'"

    def query(self, sql, copy_data=None):
        """Run one simple query (may contain several statements).

        Returns the rows (lists of str/None) of the last result set. Raises
        ``PgError`` after the server is ready again, so the connection stays
        usable (an explicit transaction is then in the failed state).
        """
        if self._sock is None:
            raise PgProtocolError('connection_closed')
        self._send(b'Q', sql.encode() + b'\0')
        rows, error = [], None
        while True:
            kind, payload = self._read()
            if kind == b'T':
                rows = []
            elif kind == b'D':
                count = struct.unpack('!H', payload[:2])[0]
                pos, row = 2, []
                for _ in range(count):
                    size = struct.unpack('!i', payload[pos:pos + 4])[0]
                    pos += 4
                    if size < 0:
                        row.append(None)
                    else:
                        row.append(payload[pos:pos + size].decode())
                        pos += size
                rows.append(row)
            elif kind == b'G':
                if copy_data is None:
                    self._send(b'f', b'no_copy_data\0')
                else:
                    self._send(b'd', copy_data)
                    self._send(b'c')
            elif kind == b'E':
                error = PgError(self._fields(payload))
            elif kind == b'Z':
                self.status = payload[:1].decode()
                if error is not None:
                    raise error
                return rows
            elif kind == b'S':
                key, value = payload.rstrip(b'\0').split(b'\0', 1)
                self.parameters[key.decode()] = value.decode()
            # C (complete), I (empty), N (notice), A (notify) are ignored.

    def scalar(self, sql):
        rows = self.query(sql)
        if len(rows) != 1 or len(rows[0]) != 1:
            raise PgProtocolError('scalar_expected')
        return rows[0][0]

    def close(self):
        if self._sock is not None:
            try:
                self._send(b'X')
            except OSError:
                pass
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
