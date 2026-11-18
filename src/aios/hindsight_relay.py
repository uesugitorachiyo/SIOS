"""Stdio MCP relay: SIOS Hindsight memory tools over the Mac's HTTPS gateway.

Python standard library only (no pip install). Runs on Windows or macOS.
Exposes the same four tools as the Mac thin MCP and forwards each call to the
gateway at ``--endpoint``; Hindsight, its database and the write journal stay on
the Mac. The bearer token is read only from the SIOS_MEMORY_GATEWAY_TOKEN
environment variable (set by the PowerShell launcher from a DPAPI file) and is
never printed or logged.

An operator supplies the endpoint and trust anchor separately.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path

TOKEN_ENV = 'SIOS_MEMORY_GATEWAY_TOKEN'
SUPPORTED = ('2025-06-18', '2025-03-26', '2024-11-05')
SERVER_INFO = {'name': 'sios-memory-relay', 'version': '1.0.0'}

_STR = {'type': 'string'}
TOOLS = [
    {'name': 'memory_recall',
     'description': ('Recall from the configured Hindsight bank, strictly filtered by one project_tag '
                     '(for example project:sios). Hits show kind, source_path and source_revision. '
                     'kind=hindsight_primary: read with memory_read_primary. kind=basic_source_copy: read the '
                     'original through the registered SIOS read route by source_path and compare its sha256 with '
                     'source_revision. kind=unknown_origin has no verified source pointer. '
                     'Hit excerpts are not authoritative.'),
     'inputSchema': {'type': 'object', 'properties': {'query': _STR, 'project_tag': _STR,
                                                      'limit': {'type': 'integer', 'default': 10}},
                     'required': ['query', 'project_tag']},
     'annotations': {'readOnlyHint': True, 'openWorldHint': True}},
    {'name': 'memory_store',
     'description': ('Store ONE new memory in Hindsight (via the Mac, the single writer). request_id must be unique '
                     'and stable per memory; reuse the same request_id only to confirm the same content. One send '
                     'only: on unknown_no_retry never resend, call memory_store_reconcile. Optional entities: up to '
                     '8 short names.'),
     'inputSchema': {'type': 'object',
                     'properties': {'content': _STR, 'request_id': _STR, 'project_tag': _STR,
                                    'supersedes': {'type': ['string', 'null']},
                                    'entities': {'type': ['array', 'null'], 'items': _STR}},
                     'required': ['content', 'request_id', 'project_tag']},
     'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': True}},
    {'name': 'memory_store_reconcile',
     'description': 'Resolve a memory_store whose outcome was unknown by an exact GET of the target. Never resends.',
     'inputSchema': {'type': 'object', 'properties': {'request_id': _STR}, 'required': ['request_id']},
     'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': True}},
    {'name': 'memory_read_primary',
     'description': ('Read a memory stored by memory_store: path Memory/hindsight/primary-<64 hex>.md. Returns text '
                     'and source_revision (sha256 of the text). Only journal-committed documents are readable.'),
     'inputSchema': {'type': 'object', 'properties': {'path': _STR}, 'required': ['path']},
     'annotations': {'readOnlyHint': True, 'openWorldHint': True}},
]
TOOL_NAMES = {t['name'] for t in TOOLS}
SAFE_ERRORS = {'invalid_request', 'invalid_content', 'invalid_query', 'invalid_arguments',
               'forbidden_scope', 'unauthorized', 'request_conflict', 'writer_busy',
               'unknown_no_retry', 'target_conflict_no_write', 'gateway_busy'}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Relay:
    def __init__(self, endpoint: str, ca_file: str | None, ca_sha256: str, timeout: float = 75.0):
        parsed = urllib.parse.urlsplit(endpoint)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.path not in ('', '/') or not ca_file):
            raise ValueError('endpoint_must_be_https')
        try:
            parsed.port
        except ValueError:
            raise ValueError('invalid_endpoint') from None
        self.endpoint = endpoint.rstrip('/')
        self.timeout = timeout
        if not isinstance(ca_sha256, str) or len(ca_sha256) != 64 or any(c not in '0123456789abcdefABCDEF' for c in ca_sha256):
            raise ValueError('invalid_ca_pin')
        with Path(ca_file).open('rb') as ca_stream:
            ca_bytes = ca_stream.read(1024 * 1024 + 1)
        if len(ca_bytes) > 1024 * 1024 or not hmac.compare_digest(hashlib.sha256(ca_bytes).hexdigest(), ca_sha256.lower()):
            raise ValueError('ca_pin_mismatch')
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.ctx.load_verify_locations(cadata=ca_bytes.decode('ascii'))
        self.ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                  _NoRedirect(),
                                                  urllib.request.HTTPSHandler(context=self.ctx))

    def forward(self, name: str, arguments: dict):
        """-> (is_error, payload dict). Error text is a static code, never a secret."""
        if not isinstance(name, str) or name not in TOOL_NAMES or not isinstance(arguments, dict):
            return True, {'error': 'invalid_tool_call'}
        token = os.environ.get(TOKEN_ENV, '')
        if not token:
            return True, {'error': 'gateway_token_missing'}
        body = json.dumps(arguments or {}, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(f'{self.endpoint}/v1/{name}', data=body, method='POST', headers={
            'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read(1048577)
                if len(raw) > 1048576:
                    return True, {'error': 'unknown_no_retry' if name == 'memory_store' else 'gateway_response_too_large'}
                data = json.loads(raw.decode('utf-8'))
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read(4097).decode('utf-8'))
                code = data.get('error') if isinstance(data, dict) else None
            except Exception:
                code = None
            code = code.split(':', 1)[0] if isinstance(code, str) else None
            if name == 'memory_store':
                return True, {'error': 'unknown_no_retry'}
            return True, {'error': code if code in SAFE_ERRORS else f'gateway_http_{exc.code}'}
        except (urllib.error.URLError, OSError, TimeoutError, ssl.SSLError):
            return True, {'error': 'unknown_no_retry' if name == 'memory_store' else 'gateway_unavailable'}
        except (ValueError, UnicodeError):
            return True, {'error': 'unknown_no_retry' if name == 'memory_store' else 'gateway_invalid_response'}
        if isinstance(data, dict) and data.get('ok') is True:
            return False, data.get('result')
        return True, {'error': 'unknown_no_retry' if name == 'memory_store' else 'gateway_invalid_response'}


def _reply(msg_id, result=None, error=None):
    out = {'jsonrpc': '2.0', 'id': msg_id}
    if error is not None:
        out['error'] = error
    else:
        out['result'] = result
    sys.stdout.write(json.dumps(out, ensure_ascii=False, separators=(',', ':')) + '\n')
    sys.stdout.flush()


def handle(relay: Relay, msg: dict):
    method, msg_id, params = msg.get('method'), msg.get('id'), msg.get('params', {})
    if msg.get('jsonrpc') != '2.0' or not isinstance(method, str):
        return _reply(None, error={'code': -32600, 'message': 'invalid_request'})
    if 'id' in msg and not (msg_id is None or type(msg_id) in (str, int)):
        return _reply(None, error={'code': -32600, 'message': 'invalid_request'})
    if 'id' not in msg:  # notification (initialized, cancelled, ...)
        return
    if not isinstance(params, dict):
        return _reply(msg_id, error={'code': -32602, 'message': 'invalid_params'})
    if method == 'initialize':
        wanted = params.get('protocolVersion')
        return _reply(msg_id, {'protocolVersion': wanted if wanted in SUPPORTED else SUPPORTED[0],
                               'capabilities': {'tools': {'listChanged': False}}, 'serverInfo': SERVER_INFO})
    if method == 'ping':
        return _reply(msg_id, {})
    if method == 'tools/list':
        return _reply(msg_id, {'tools': TOOLS})
    if method == 'tools/call':
        name, arguments = params.get('name'), params.get('arguments')
        if not isinstance(name, str) or name not in TOOL_NAMES or not isinstance(arguments, dict):
            return _reply(msg_id, error={'code': -32602, 'message': 'invalid_tool_call'})
        is_error, payload = relay.forward(name, arguments)
        text = payload['error'] if is_error else json.dumps(payload, ensure_ascii=False, indent=2)
        return _reply(msg_id, {'content': [{'type': 'text', 'text': text}], 'isError': is_error})
    return _reply(msg_id, error={'code': -32601, 'message': 'method_not_found'})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='sios_memory_relay')
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--ca-file')
    parser.add_argument('--ca-sha256', required=True)
    args = parser.parse_args(argv)
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding='utf-8', newline='\n')
        except (AttributeError, ValueError):
            pass
    try:
        relay = Relay(args.endpoint, args.ca_file, args.ca_sha256)
    except (ValueError, OSError, ssl.SSLError) as exc:
        print(f'relay_configuration_invalid: {type(exc).__name__}', file=sys.stderr)
        return 2
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            _reply(None, error={'code': -32700, 'message': 'parse_error'})
            continue
        if isinstance(msg, dict):
            handle(relay, msg)
        else:
            _reply(None, error={'code': -32600, 'message': 'invalid_request'})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
