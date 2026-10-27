"""Local, read-only browser interface for an existing SIOS client."""
from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import os
import secrets
import sys
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .client import Client
from .config import Config
from .evidence_search import search_evidence

MAX_REQUEST = 8192
MAX_SOURCE_BYTES = 10_000_000
MAX_RESPONSE_BYTES = 16_000_000
SEARCH_SECONDS = 45
_SEARCH_DEADLINE = contextvars.ContextVar('sios_search_deadline', default=None)


def mcp_value(result, operation):
    """Decode a native MCP result without exposing server error details."""
    if getattr(result, 'is_error', False):
        raise RuntimeError('SIOS operation unavailable')
    structured = getattr(result, 'structured_content', None)
    if structured is not None:
        return structured
    blocks = getattr(result, 'content', [])
    texts = [getattr(item, 'text', None) for item in blocks if getattr(item, 'type', None) == 'text']
    if operation == 'search':
        if len(texts) != len(blocks):
            raise RuntimeError('SIOS response unavailable')
        try:
            values = [json.loads(text) for text in texts]
        except (ValueError, TypeError):
            raise RuntimeError('SIOS response unavailable') from None
        if len(values) == 1 and isinstance(values[0], list):
            return values[0]
        if all(isinstance(value, dict) for value in values):
            return values
        raise RuntimeError('SIOS response unavailable')
    if len(texts) != 1:
        raise RuntimeError('SIOS response unavailable')
    try:
        return json.loads(texts[0])
    except (ValueError, TypeError):
        raise RuntimeError('SIOS response unavailable') from None


def registered_launcher(name, config_path=None):
    """Read one existing SIOS launcher definition without printing its environment."""
    if name not in {'sios-l0', 'sios-nucx'}:
        raise ValueError('unsupported registered client')
    config_path = config_path or Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'config.toml'
    with Path(config_path).open('rb') as stream:
        document = tomllib.load(stream)
    entry = document.get('mcp_servers', {}).get(name)
    if not isinstance(entry, dict) or not isinstance(entry.get('command'), str) or not entry['command']:
        raise ValueError('registered client unavailable')
    args = entry.get('args', [])
    env = entry.get('env', {})
    cwd = entry.get('cwd')
    if (not isinstance(args, list) or not all(isinstance(x, str) for x in args) or
        not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()) or
        (cwd is not None and not isinstance(cwd, str))):
        raise ValueError('invalid registered client')
    return entry['command'], args, env, cwd


async def serve_mcp(command, arguments, port, env=None, cwd=None):
    """Use an existing configured MCP launcher; the launcher handles credentials."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=command, args=arguments, env=env, cwd=cwd)
    with open(os.devnull, 'w', encoding='utf-8') as errors:
        async with stdio_client(params, errlog=errors) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                loop = asyncio.get_running_loop()
                def call(operation, arguments):
                    future = asyncio.run_coroutine_threadsafe(session.call_tool(operation, arguments), loop)
                    try:
                        deadline = _SEARCH_DEADLINE.get()
                        timeout = min(60, max(0.01, deadline - time.monotonic())) if deadline is not None else 60
                        return mcp_value(future.result(timeout=timeout), operation)
                    except TimeoutError:
                        future.cancel()
                        raise RuntimeError('SIOS operation timed out') from None
                server = create_server(call, port=port)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                print('Evidence Search: ' + server.access_url, flush=True)
                try:
                    await asyncio.Event().wait()
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join()


def create_server(call, *, port=0):
    """Bind only to loopback and return a server without starting it."""
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError('invalid port')
    csrf = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(24)
    search_lock = threading.BoundedSemaphore(1)
    template = Path(__file__).with_name('evidence_search_ui.html').read_text(encoding='utf-8')
    page = template.replace('__NONCE__', nonce).encode('utf-8')

    class Handler(BaseHTTPRequestHandler):
        server_version = 'SIOS'
        sys_version = ''

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, format, *args):
            pass  # Search terms and private source paths must not enter logs.

        def _host(self):
            return '127.0.0.1:' + str(self.server.server_address[1])

        def _send(self, status, data, content_type):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-" + nonce + "'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.headers.get('Host') != self._host() or self.path != '/':
                self._send(404, b'Not found', 'text/plain; charset=utf-8')
                return
            self._send(200, page, 'text/html; charset=utf-8')

        def do_POST(self):
            expected_origin = 'http://' + self._host()
            if (self.path != '/search' or self.headers.get('Host') != self._host() or
                self.headers.get('Origin') != expected_origin or
                not secrets.compare_digest(self.headers.get('X-SIOS-CSRF', ''), csrf) or
                self.headers.get_content_type() != 'application/json'):
                self._send(403, b'Forbidden', 'text/plain; charset=utf-8')
                return
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) != 1 or not lengths[0].isdigit() or not 0 < int(lengths[0]) <= MAX_REQUEST:
                self._send(400, b'Invalid request', 'text/plain; charset=utf-8')
                return
            try:
                data = json.loads(self.rfile.read(int(lengths[0])).decode('utf-8'))
                if not isinstance(data, dict) or set(data) != {'query'}:
                    raise ValueError('invalid request')
                if not search_lock.acquire(blocking=False):
                    self._send(429, b'Search already in progress', 'text/plain; charset=utf-8')
                    return
                used = 0
                limited = False
                timed_out = False
                deadline = time.monotonic() + SEARCH_SECONDS
                def bounded_call(operation, params):
                    nonlocal used, limited, timed_out
                    if time.monotonic() >= deadline:
                        timed_out = True
                        raise RuntimeError('search time limit')
                    if operation == 'read' and (limited or used >= MAX_SOURCE_BYTES):
                        limited = True
                        raise RuntimeError('source limit')
                    result = call(operation, params)
                    if operation == 'read' and isinstance(result, dict) and isinstance(result.get('text'), str):
                        size = len(result['text'].encode('utf-8'))
                        if used + size > MAX_SOURCE_BYTES:
                            limited = True
                            raise RuntimeError('source limit')
                        used += size
                    return result
                try:
                    token = _SEARCH_DEADLINE.set(deadline)
                    try:
                        result = search_evidence(bounded_call, data['query'], limit=30)
                    finally:
                        _SEARCH_DEADLINE.reset(token)
                finally:
                    search_lock.release()
                if limited:
                    result['truncated'] = True
                    result['warnings'].append({'store': 'all', 'reason': 'response_limit'})
                if timed_out or time.monotonic() >= deadline:
                    result['truncated'] = True
                    result['warnings'].append({'store': 'all', 'reason': 'time_limit'})
                body = json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')
                if len(body) > MAX_RESPONSE_BYTES:
                    result['truncated'] = True
                    if {'store': 'all', 'reason': 'response_limit'} not in result['warnings']:
                        result['warnings'].append({'store': 'all', 'reason': 'response_limit'})
                    while len(body) > MAX_RESPONSE_BYTES and result['results']:
                        result['results'].pop()
                        body = json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise ValueError('response limit')
            except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
                self._send(400, b'Invalid request', 'text/plain; charset=utf-8')
                return
            self._send(200, body, 'application/json; charset=utf-8')

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = False

    server = Server(('127.0.0.1', port), Handler)
    server.access_url = 'http://127.0.0.1:' + str(server.server_address[1]) + '/#' + csrf
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--config', type=Path, help='Existing SIOS client configuration outside the source checkout')
    target.add_argument('--mcp-command', help='Existing SIOS MCP launcher executable')
    target.add_argument('--registered', choices=('sios-l0', 'sios-nucx'), help='Use an existing registered SIOS MCP client')
    parser.add_argument('--mcp-arg', action='append', default=[], help='Argument for the existing MCP launcher; repeat as needed')
    parser.add_argument('--ca-file', type=Path, help='Existing trusted CA for the SIOS server')
    parser.add_argument('--port', type=int, default=0, help='Local loopback port; 0 chooses an available port')
    args = parser.parse_args(argv)
    if args.mcp_command or args.registered:
        try:
            if args.registered:
                if args.mcp_arg:
                    raise ValueError('registered client does not accept extra arguments')
                command, arguments, env, cwd = registered_launcher(args.registered)
            else:
                command, arguments, env, cwd = args.mcp_command, args.mcp_arg, None, None
            asyncio.run(serve_mcp(command, arguments, args.port, env=env, cwd=cwd))
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            print('Unable to start evidence search: ' + type(exc).__name__, file=sys.stderr)
            return 2
        return 0
    try:
        config = Config.load(args.config)
        if config.mode != 'client':
            raise ValueError('an existing client configuration is required')
        client = Client(config, ca_file=args.ca_file, timeout=10)
        server = create_server(client.call, port=args.port)
    except (OSError, ValueError) as exc:
        print('Unable to start evidence search: ' + type(exc).__name__, file=sys.stderr)
        return 2
    print('Evidence Search: ' + server.access_url, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
