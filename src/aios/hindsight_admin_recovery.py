"""Local administrative recovery, gated by a trusted host's live writer exclusion.

The caller can supply a request ID only. The host supplies reviewed request
bytes, the destination binding, and an exclusive guard covering old writers.
"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .hindsight_unix_recovery import UnixRecoveryTarget


class RecoveryRefused(ValueError):
    pass


@dataclass(frozen=True)
class RecoveryGrant:
    request_id: str
    operator: str
    endpoint: str
    bank: str
    journal_root: Path
    payload: bytes
    payload_sha256: str
    coverage_complete: bool
    routes: frozenset[str]
    blocked_routes: frozenset[str]
    prove_live: Callable[[], bool]
    prove_absent_safe: Callable[[], bool]
    reservation_root: Path | None = None


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _reserve(root, request_id, record):
    path = root / ('recovery-' + digest(request_id.encode()) + '.json')
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        raise RecoveryRefused('recovery_already_reserved') from None
    with os.fdopen(descriptor, 'w') as out:
        json.dump(record, out, sort_keys=True, allow_nan=False)
        out.flush()
        os.fsync(out.fileno())
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def recover(target, operator, request_id, *, authority, locked, bank):
    """Attempt recovery once. On any uncertainty retain the existing journal row."""
    from .hindsight_writer import reconcile

    with authority.hold(target, operator, request_id) as grant:
        if (not isinstance(grant, RecoveryGrant) or grant.coverage_complete is not True
                or not grant.routes or grant.routes != grant.blocked_routes
                or grant.request_id != request_id or grant.operator != operator
                or grant.endpoint != target.endpoint or grant.bank != bank
                or grant.journal_root != target.journal_root
                or not grant.prove_live()):
            raise RecoveryRefused('old_routes_not_fenced')
        if isinstance(target, UnixRecoveryTarget):
            valid_endpoint = grant.endpoint == 'unix:' + str(target.socket_path)
        else:
            endpoint = urlsplit(grant.endpoint)
            valid_endpoint = endpoint.scheme in ('http', 'https') and endpoint.hostname in ('127.0.0.1', '::1')
        if not valid_endpoint:
            raise RecoveryRefused('invalid_recovery_endpoint')
        if len(grant.payload) > 600 * 1024 or digest(grant.payload) != grant.payload_sha256:
            raise RecoveryRefused('recovery_payload_mismatch')
        payload = json.loads(grant.payload)
        if (not isinstance(payload, dict) or set(payload) != {'items', 'async'}
                or payload['async'] is not False or not isinstance(payload['items'], list)
                or len(payload['items']) != 1 or not isinstance(payload['items'][0], dict)):
            raise RecoveryRefused('recovery_payload_mismatch')
        item = payload['items'][0]
        with locked(target.journal_root, target.lock_wait) as journal:
            row = journal.get(request_id)
            ident = 'primary-' + digest(request_id.encode())
            content = item.get('content')
            if (not row or row['document_id'] != ident or item.get('document_id') != ident
                    or item.get('metadata') != row['metadata'] or not isinstance(content, str)
                    or digest(content.encode()) != row['metadata'].get('content_sha256')
                    or item.get('strategy') != 'sios_primary_chunks'):
                raise RecoveryRefused('recovery_payload_mismatch')
            actor = row['metadata']['source_actor']
            client = target.client(timeout=target.write_timeout)
            existing = client.get(ident)
            if existing is not None:
                return reconcile(client, journal, request_id=request_id, actor=actor)
            if not grant.prove_absent_safe():
                raise RecoveryRefused('prior_effects_unverified')
            if row['state'] != 'unknown':
                raise RecoveryRefused('recovery_not_unknown')
            strategies = client._request('/config').get('config', {}).get('retain_strategies', {})
            if strategies.get('sios_primary_chunks') != {'retain_extraction_mode': 'chunks'}:
                raise RecoveryRefused('retain_strategy_unconfigured')
            if not grant.prove_live():
                raise RecoveryRefused('old_routes_not_fenced')
            _reserve(grant.reservation_root or target.journal_root, request_id, {
                'request_id': request_id, 'document_id': ident, 'payload_sha256': grant.payload_sha256,
                'operator': operator, 'bank': bank,
            })
            # A fault even before the retain leaves the reservation in place.
            if not grant.prove_live():
                raise RecoveryRefused('old_routes_not_fenced')
            try:
                client._request('/memories', payload)
            except Exception:
                return {'status': 'unknown_no_retry', 'document_id': ident}
            if not grant.prove_live():
                return {'status': 'unknown_no_retry', 'document_id': ident}
            return reconcile(client, journal, request_id=request_id, actor=actor)
