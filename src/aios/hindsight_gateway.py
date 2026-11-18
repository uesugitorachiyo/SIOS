"""HTTPS transport for an explicitly supplied Hindsight memory backend.

This module does not implement or configure a backend or install a service.
A qualified backend must authenticate and authorize each operation, preserve
its single-writer journal and reconcile uncertain writes without resending.
A project tag is a search filter, never an authorization boundary.
"""
from __future__ import annotations

import hashlib
import fcntl
import hmac
import ipaddress
import json
import os
import re
import secrets
import ssl
import stat
import socket
import sys
import threading
import time
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAX_BODY_BYTES = 600 * 1024
MAX_CONCURRENT = 8
SOCKET_TIMEOUT = 75.0
AUTH_DEADLINE = 5.0
DEVICE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
VALID_SCOPES = frozenset({'read', 'memory:write'})
ERROR_CODES = frozenset({'invalid_request_id', 'invalid_content', 'invalid_actor', 'invalid_project_tag',
                         'invalid_supersedes', 'invalid_entities', 'invalid_query', 'invalid_limit',
                         'invalid_path', 'invalid_source', 'invalid_results', 'invalid_operation',
                         'request_conflict', 'reconcile_actor_mismatch', 'journal_unavailable',
                         'journal_transition_invalid', 'writer_busy', 'unknown_no_retry', 'internal_error'})
MIN_PORT = 1024

# endpoint -> (required scope, allowed argument names)
OPERATIONS = {
    'memory_recall': ('read', {'query', 'project_tag', 'limit'}),
    'memory_read_primary': ('read', {'path'}),
    'memory_store': ('memory:write', {'content', 'request_id', 'project_tag', 'supersedes', 'entities'}),
    'memory_store_reconcile': ('memory:write', {'request_id'}),
}


class GatewayConfigError(Exception):
    pass


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode('ascii')).hexdigest()


# -- configuration -----------------------------------------------------------
def _check_private(path: Path, mode: int, directory: bool = False) -> None:
    info = path.lstat()
    ok_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not ok_type or stat.S_ISLNK(info.st_mode) or (not directory and info.st_nlink != 1) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != mode):
        raise GatewayConfigError(f'private_path_invalid: {path.name}')


def _check_certificate(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022):
        raise GatewayConfigError('certificate_path_invalid')


def load_config(path: Path, *, require_files: bool = True) -> dict:
    path = Path(path)
    try:
        _check_private(path.parent, 0o700, directory=True)
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'r', encoding='utf-8') as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise GatewayConfigError('config_invalid_permissions')
            cfg = json.load(handle)
    except (OSError, ValueError):
        raise GatewayConfigError('config_unreadable') from None
    if not isinstance(cfg, dict):
        raise GatewayConfigError('config_invalid')
    host, port = cfg.get('host'), cfg.get('port')
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        raise GatewayConfigError('host_invalid') from None
    if addr.version != 4 or not (addr.is_private or addr.is_loopback) or addr.is_unspecified:
        raise GatewayConfigError('host_must_be_one_private_ipv4')
    if type(port) is not int or not MIN_PORT <= port <= 65535:
        raise GatewayConfigError('port_invalid')
    devices = cfg.get('devices')
    if not isinstance(devices, dict):
        raise GatewayConfigError('devices_invalid')
    for name, entry in devices.items():
        if (not DEVICE.fullmatch(str(name)) or not isinstance(entry, dict)
                or not re.fullmatch(r'[0-9a-f]{64}', str(entry.get('token_sha256', '')))
                or not isinstance(entry.get('scopes'), list) or not set(entry['scopes']) <= VALID_SCOPES
                or not DEVICE.fullmatch(str(entry.get('actor', '')))):
            raise GatewayConfigError('device_invalid')
    if require_files:
        for key in ('cert_file', 'key_file'):
            value = cfg.get(key)
            if not isinstance(value, str) or not Path(value).is_absolute() or not Path(value).is_file():
                raise GatewayConfigError(f'{key}_invalid')
        _check_private(Path(cfg['key_file']), 0o600)
        _check_certificate(Path(cfg['cert_file']))
        _check_private(Path(cfg['key_file']).parent, 0o700, directory=True)
        _check_private(Path(cfg['cert_file']).parent, 0o700, directory=True)
    return cfg


def _save_config_locked(path: Path, cfg: dict) -> None:
    """Internal replacement; caller holds the configuration mutation lock."""
    path = Path(path)
    _check_private(path.parent, 0o700, directory=True)
    fd, name = tempfile.mkstemp(prefix='.gateway-config-', dir=path.parent)
    tmp = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(cfg, handle, indent=2, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        dirfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if tmp.exists():
            tmp.unlink()


def init_device(config_path: Path, name: str, scopes: list, token_out: Path, actor: str | None = None) -> None:
    """Create one device; a failed or uncertain save requires operator reconciliation."""
    if (not isinstance(name, str) or not DEVICE.fullmatch(name) or not set(scopes) <= VALID_SCOPES or not scopes
            or (actor is not None and (not isinstance(actor, str) or not DEVICE.fullmatch(actor)))):
        raise GatewayConfigError('device_invalid')
    config_path = Path(config_path)
    _check_private(config_path.parent, 0o700, directory=True)
    lock = config_path.parent / '.gateway-config.lock'
    lock_fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        _check_private(lock, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        cfg = load_config(config_path, require_files=False)
        if name in cfg['devices']:
            raise GatewayConfigError('device_already_exists')
        _check_private(Path(token_out).parent, 0o700, directory=True)
        token = secrets.token_urlsafe(48)
        fd = os.open(token_out, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'w', encoding='ascii') as handle:
            handle.write(token)
            handle.flush()
            os.fsync(handle.fileno())
        cfg['devices'][name] = {'token_sha256': token_digest(token), 'scopes': sorted(scopes),
                                'actor': actor or name}
        _save_config_locked(config_path, cfg)
    finally:
        os.close(lock_fd)


# -- backend (the real Hindsight thin MCP functions) --------------------------
# -- HTTP server --------------------------------------------------------------
class Gateway(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, cfg: dict, backend, *, ssl_context: ssl.SSLContext | None = None):
        self.cfg, self.backend = cfg, backend
        self.devices = cfg['devices']
        self.slots = threading.BoundedSemaphore(MAX_CONCURRENT)
        self.connection_slots = threading.BoundedSemaphore(MAX_CONCURRENT)
        super().__init__((cfg['host'], cfg['port']), _Handler)
        if ssl_context is not None:
            self.socket = ssl_context.wrap_socket(self.socket, server_side=True, do_handshake_on_connect=False)

    def process_request(self, request, client_address):
        if not self.connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connection_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connection_slots.release()

    def handle_error(self, request, client_address):
        # Never print client-controlled input, internal paths or a traceback.
        sys.stderr.write('gateway_connection_error\n')

    def authenticate(self, header: str | None):
        """Constant-time scan over all devices. Returns (name, entry) or None."""
        if not header or not header.startswith('Bearer ') or len(header) > 512:
            return None
        try:
            digest = token_digest(header[7:].strip())
        except UnicodeEncodeError:
            return None
        found = None
        for name, entry in self.devices.items():
            if hmac.compare_digest(digest, entry['token_sha256']) and found is None:
                found = (name, entry)
        return found


class _Handler(BaseHTTPRequestHandler):
    server_version = 'sios-hindsight-gateway'
    sys_version = ''
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(SOCKET_TIMEOUT)
        def expire():
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._auth_timer = threading.Timer(AUTH_DEADLINE, expire)
        self._auth_timer.daemon = True
        self._auth_timer.start()

    def finish(self):
        self._auth_timer.cancel()
        super().finish()

    def log_message(self, fmt, *args):  # suppress default access log (it may include the path only, but be strict)
        return

    def _audit(self, device, status):
        allowed_paths = {'/v1/health'} | {'/v1/' + op for op in OPERATIONS}
        path = self.path if self.path in allowed_paths else '<unknown>'
        sys.stderr.write(f'{time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())} device={device or "-"} '
                         f'{self.command} {path} status={status}\n')
        sys.stderr.flush()

    def _send(self, status, payload, device=None):
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        # Every request is separately authenticated; never reuse the connection.
        self.close_connection = True
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)
        self._audit(device, status)

    def do_GET(self):
        if self.path != '/v1/health':
            return self._send(404, {'ok': False, 'error': 'not_found'})
        auth = self.server.authenticate(self.headers.get('Authorization'))
        if auth is None:
            return self._send(401, {'ok': False, 'error': 'unauthorized'})
        self._auth_timer.cancel()
        return self._send(200, self.server.backend.health(), auth[0])

    def do_POST(self):
        if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
            self.close_connection = True
            return self._send(400, {'ok': False, 'error': 'invalid_framing'})
        auth = self.server.authenticate(self.headers.get('Authorization'))
        if auth is None:
            self.close_connection = True
            return self._send(401, {'ok': False, 'error': 'unauthorized'})
        self._auth_timer.cancel()
        device, entry = auth
        op = self.path[len('/v1/'):] if self.path.startswith('/v1/') else None
        if op not in OPERATIONS:
            return self._send(404, {'ok': False, 'error': 'not_found'}, device)
        scope, allowed = OPERATIONS[op]
        if scope not in entry['scopes']:
            return self._send(403, {'ok': False, 'error': 'forbidden_scope'}, device)
        length = self.headers.get('Content-Length')
        if not length or not length.isdigit():
            self.close_connection = True
            return self._send(411, {'ok': False, 'error': 'length_required'}, device)
        if len(length) > 9 or int(length) > MAX_BODY_BYTES:
            self.close_connection = True
            return self._send(413, {'ok': False, 'error': 'body_too_large'}, device)
        raw = self.rfile.read(int(length))
        if len(raw) != int(length):
            self.close_connection = True
            return self._send(400, {'ok': False, 'error': 'invalid_framing'}, device)
        try:
            args = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeError):
            return self._send(400, {'ok': False, 'error': 'invalid_json'}, device)
        if not isinstance(args, dict) or not set(args) <= allowed:
            return self._send(400, {'ok': False, 'error': 'invalid_arguments'}, device)
        if not self.server.slots.acquire(timeout=5):
            return self._send(503, {'ok': False, 'error': 'gateway_busy'}, device)
        try:
            result = self.server.backend.call(op, entry['actor'], args)
        except Exception as exc:  # mapped to a static code; details never leave the Mac
            status, text = self.server.backend.map_error(exc)
            if type(status) is not int or status < 400 or status > 599 or text not in ERROR_CODES:
                status, text = 500, 'internal_error'
            return self._send(status, {'ok': False, 'error': text}, device)
        finally:
            self.server.slots.release()
        return self._send(200, {'ok': True, 'result': result}, device)


def build_ssl_context(cfg: dict) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cfg['cert_file'], cfg['key_file'])
    return ctx


def serve(config_path: Path, backend) -> None:
    """Serve only with an explicitly supplied, separately qualified backend."""
    cfg = load_config(config_path)
    server = Gateway(cfg, backend, ssl_context=build_ssl_context(cfg))
    try:
        server.serve_forever()
    finally:
        server.server_close()
