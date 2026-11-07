"""Opt-in stdio client for an already provisioned forced-command SSH gateway.

The gateway must authenticate, authorize, review, journal and reconcile writes.
This client never retries an uncertain request and does not provide the gateway.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import threading
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

PATH = re.compile(r'Memory/notes/selective-[0-9a-f]{64}\.md\Z')
REVISION = re.compile(r'[0-9a-f]{64}\Z')
HOST = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}@[A-Za-z0-9][A-Za-z0-9.-]{0,252}\Z')
ALIAS = re.compile(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}\Z')
STATES = frozenset({'inspected', 'verified', 'historically_verified', 'no_attempt', 'prior_attempt',
    'unknown', 'unknown_review_unconfirmed', 'unknown_source_changed', 'unconfirmed', 'not_submitted',
    'deferred_correction_review', 'deferred_existing_source_or_revision', 'deferred_id_collision',
    'deferred_legacy_identity_unknown', 'deferred_operation_active', 'deferred_review',
    'deferred_scope_or_provenance', 'deferred_sensitive_content'})


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate field')
        result[key] = value
    return result


def _file(value):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError('absolute regular file required')
    for part in reversed((path, *path.parents)):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('reparse point')
    if not path.is_file():
        raise ValueError('absolute regular file required')
    return str(path)


class SelectedSync:
    def __init__(self, *, ssh, key, known_hosts, host, hostkey_alias, port=22,
                 timeout=255, popen=subprocess.Popen):
        self.ssh, self.key, self.known_hosts = _file(ssh), _file(key), _file(known_hosts)
        if (not isinstance(host, str) or not HOST.fullmatch(host) or
                not isinstance(hostkey_alias, str) or not ALIAS.fullmatch(hostkey_alias) or
                type(port) is not int or not 1 <= port <= 65535 or
                isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 255):
            raise ValueError('invalid SSH configuration')
        self.host, self.alias, self.port, self.timeout, self.popen = host, hostkey_alias, port, timeout, popen

    def invoke(self, mode, path, revision=None, selected_reviewed=False):
        if mode not in ('inspect', 'submit', 'reconcile') or not isinstance(path, str) or not PATH.fullmatch(path):
            return {'state': 'invalid_request', 'automatic_retry': False}
        request = {'mode': mode, 'path': path}
        if mode != 'inspect':
            if not isinstance(revision, str) or not REVISION.fullmatch(revision) or selected_reviewed is not True:
                return {'state': 'invalid_request', 'automatic_retry': False}
            request.update(revision=revision, selected_reviewed=True)
        cmd = [self.ssh, '-F', 'NUL', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'HostKeyAlias=' + self.alias, '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none',
               '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes', '-o', 'GlobalKnownHostsFile=NUL',
               '-o', 'UpdateHostKeys=no', '-o', 'UserKnownHostsFile=' + self.known_hosts,
               '-o', 'ConnectTimeout=8', '-p', str(self.port), '-i', self.key, self.host]
        try:
            proc = self.popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            return {'state': 'not_started', 'automatic_retry': False}
        output, failure = bytearray(), []

        def receive():
            try:
                while True:
                    chunk = proc.stdout.read(4096)
                    if not chunk:
                        break
                    if len(output) + len(chunk) > 16384:
                        failure.append('oversize')
                        proc.kill()
                        break
                    output.extend(chunk)
            except (OSError, ValueError):
                failure.append('read_error')
                try: proc.kill()
                except OSError: pass

        reader = threading.Thread(target=receive, daemon=True)
        try:
            reader.start()
        except RuntimeError:
            try: proc.kill()
            except OSError: pass
            return {'state': 'unknown', 'automatic_retry': False}
        try:
            proc.stdin.write((json.dumps(request) + '\n').encode('utf-8'))
            proc.stdin.close()
            proc.wait(timeout=self.timeout)
            reader.join(timeout=5)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            return {'state': 'unknown', 'automatic_retry': False}
        if reader.is_alive() or failure:
            return {'state': 'unknown', 'automatic_retry': False}
        if proc.returncode:
            return {'state': 'unconfirmed', 'automatic_retry': False}
        try:
            value = json.loads(output, object_pairs_hook=_unique)
        except (ValueError, UnicodeError):
            return {'state': 'unconfirmed', 'automatic_retry': False}
        if not isinstance(value, dict) or not isinstance(value.get('state'), str) or value['state'] not in STATES:
            return {'state': 'unconfirmed', 'automatic_retry': False}
        result = {'state': value['state'], 'automatic_retry': False}
        for name in ('project_supported', 'correction_deferred', 'submitted', 'existing_documents_intentionally_modified'):
            if type(value.get(name)) is bool:
                result[name] = value[name]
        for name, pattern in (('path', PATH), ('predecessor_path', PATH), ('revision', REVISION),
                              ('predecessor_revision', REVISION), ('document_id', re.compile(r'sios-manual-[0-9a-f]{64}\Z'))):
            if isinstance(value.get(name), str) and pattern.fullmatch(value[name]):
                result[name] = value[name]
        if value.get('predecessor_read') == 'confirmed': result['predecessor_read'] = 'confirmed'
        if value.get('writer_fence') == 'this_cli_only': result['writer_fence'] = 'this_cli_only'
        if value.get('inventory') == 'non_atomic': result['inventory'] = 'non_atomic'
        return result


def build(sync):
    server = MCPServer('selected-sios-sync', instructions='Explicitly selected and reviewed records only. Unknown never permits resubmission.')
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)

    @server.tool(annotations=read)
    def selected_memory_inspect(path: str):
        """Inspect one selective source; no submission."""
        return sync.invoke('inspect', path)

    @server.tool(annotations=write)
    def selected_memory_submit(path: str, revision: str, selected_reviewed: bool):
        """Submit one explicitly reviewed source; never resubmit after an unknown result."""
        return sync.invoke('submit', path, revision, selected_reviewed)

    @server.tool(annotations=write)
    def selected_memory_reconcile(path: str, revision: str, selected_reviewed: bool):
        """Ask the existing gateway to reconcile; never submit again."""
        return sync.invoke('reconcile', path, revision, selected_reviewed)

    return server


def _run():
    if os.name != 'nt':
        raise RuntimeError('windows_client_required')
    parser = argparse.ArgumentParser()
    parser.add_argument('--ssh', required=True)
    parser.add_argument('--key', required=True)
    parser.add_argument('--known-hosts', required=True)
    parser.add_argument('--host', required=True)
    parser.add_argument('--hostkey-alias', required=True)
    parser.add_argument('--port', type=int, default=22)
    args = parser.parse_args()
    build(SelectedSync(ssh=args.ssh, key=args.key, known_hosts=args.known_hosts,
                       host=args.host, hostkey_alias=args.hostkey_alias, port=args.port)).run()


def main():
    try:
        _run()
    except Exception:
        print('sios_selected_sync_startup_failed', file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
