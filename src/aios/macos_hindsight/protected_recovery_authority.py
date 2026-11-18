"""Bind an approved recovery request to the protected writer supervisor."""

from contextlib import contextmanager
import hmac
import json
import os
import pwd
import re
import stat
import time
from types import SimpleNamespace

from ..hindsight_admin_recovery import RecoveryGrant, RecoveryRefused, digest, recover
from .protected_host_binding import ROOT, STATE, bind
from .protected_recovery_journal import REQUEST, owner_journal
from .recovery_socket_candidate import MAX_BODY, canonical, read_approval, read_gate_key
from .shadow_restore_verification import fingerprint, read_only_snapshot
from ..hindsight_unix_recovery import UnixRecoveryTarget
from .verify_sealed_handoff import _private_file, _protected_path


OPERATOR = 'local-macos-administrator'
BANK = 'sios-memory'
ROUTES = frozenset({'retired-postgres', 'retired-hindsight', 'ordinary-application-peer'})
HEX = re.compile(r'[0-9a-f]{64}\Z')


def _read(path, limit):
    with _private_file(path) as file:
        data = file.read(limit + 1)
    if len(data) > limit:
        raise ValueError('administrator review file exceeds its size limit')
    return data


def _root_directory(path):
    _protected_path(path)
    if stat.S_IMODE(path.lstat().st_mode) != 0o700:
        raise PermissionError('the recovery administrator directory is not private')


def state_fingerprint(connection):
    """Hash application relations and extension state in a read-only snapshot."""
    with read_only_snapshot(connection):
        schemas = tuple(row[0] for row in connection.query(
            "SELECT DISTINCT n.nspname FROM pg_catalog.pg_namespace n "
            "JOIN pg_catalog.pg_class c ON c.relnamespace=n.oid "
            "WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema' "
            "AND c.relkind IN ('r','p','f','m','v','S') ORDER BY 1"
        ))
        if 'sios_memory' not in schemas:
            raise ValueError('the protected Hindsight schema is missing')
        result = fingerprint(connection, schemas)
        for section in ('rows', 'sequence_state'):
            result[section] = [(key, value) for key, value in sorted(result[section].items())]
        return digest(json.dumps(result, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii'))


class ProtectedRecovery:
    def __init__(self, supervisor, *, state, service, journal, application_user, recovery_user):
        self.supervisor = supervisor
        self.state = state
        self.service = service
        self.journal = journal
        self.application_user = application_user
        self.recovery_user = recovery_user

    def _review(self, request_id):
        if not isinstance(request_id, str) or not REQUEST.fullmatch(request_id):
            raise RecoveryRefused('invalid_request_id')
        _root_directory(self.state)
        try:
            raw = _read(self.state / 'recovery-review.json', MAX_BODY + 8192)
        except FileNotFoundError:
            raise RecoveryRefused('no_approved_recovery') from None
        review, _ = canonical(raw)
        if (not isinstance(review, dict)
                or set(review) != {'request_id', 'target_cluster', 'payload', 'pre_attempt_receipt_sha256'}
                or review['request_id'] != request_id
                or review['target_cluster'] != self.supervisor.cluster):
            raise RecoveryRefused('no_approved_recovery')
        _, payload = canonical(json.dumps(review['payload'], ensure_ascii=True).encode('ascii'))
        if len(payload) > MAX_BODY:
            raise RecoveryRefused('recovery_payload_mismatch')
        return digest(raw), payload, review['pre_attempt_receipt_sha256']

    def _past_state(self, request_id, payload_hash, expected_sha):
        if expected_sha is None:
            return None
        if not isinstance(expected_sha, str) or not HEX.fullmatch(expected_sha):
            raise RecoveryRefused('invalid_pre_attempt_receipt')
        directory = self.state / 'pre-attempt'
        _root_directory(directory)
        raw = _read(directory / (digest(request_id.encode('ascii')) + '.json'), 8192)
        if digest(raw) != expected_sha:
            raise RecoveryRefused('invalid_pre_attempt_receipt')
        receipt, _ = canonical(raw)
        if (not isinstance(receipt, dict)
                or set(receipt) != {'request_id', 'target_cluster', 'state', 'fingerprint', 'payload_sha256'}
                or receipt['state'] != 'captured_before_dispatch'
                or receipt['request_id'] != request_id
                or receipt['target_cluster'] != self.supervisor.cluster
                or receipt['payload_sha256'] != payload_hash
                or not isinstance(receipt['fingerprint'], str) or not HEX.fullmatch(receipt['fingerprint'])):
            raise RecoveryRefused('invalid_pre_attempt_receipt')
        return receipt['fingerprint']

    def run(self, request_id):
        if os.getuid() != 0 or os.geteuid() != 0:
            raise PermissionError('macOS administrator identity required')
        review_digest, payload, pin = self._review(request_id)
        past_state = self._past_state(request_id, digest(payload), pin)
        reservations = self.state / 'reservations'
        _root_directory(reservations)
        _protected_path(self.service.parent)
        info = self.service.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != self.recovery_user.pw_uid
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise PermissionError('unsafe recovery service directory')
        key = read_gate_key(self.service, self.recovery_user.pw_uid)
        target = UnixRecoveryTarget(self.service / 'api.sock', self.journal, key)

        def prepare():
            if self._review(request_id)[0] != review_digest:
                raise RecoveryRefused('approved_recovery_changed')
            _, service_payload = canonical(read_approval(self.service, self.recovery_user.pw_uid))
            if (not hmac.compare_digest(payload, service_payload)
                    or not hmac.compare_digest(key, read_gate_key(self.service, self.recovery_user.pw_uid))):
                raise RecoveryRefused('review_and_recovery_service_differ')
            try:
                info = target.socket_path.lstat()
            except FileNotFoundError:
                return
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.recovery_user.pw_uid:
                raise RecoveryRefused('unexpected_recovery_socket')
            target.socket_path.unlink()

        def prove_absent_safe():
            if past_state is None:
                return False
            with self.supervisor.connect() as connection:
                return hmac.compare_digest(past_state, state_fingerprint(connection))

        @contextmanager
        def hold(bound_target, operator, identifier):
            if bound_target is not target or operator != OPERATOR or identifier != request_id:
                raise RecoveryRefused('invalid_recovery_authority')
            with self.supervisor.recovery(prepare=prepare) as prove_live:
                deadline = time.monotonic() + 25
                while True:
                    try:
                        target.client(2)._request('/config')
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise RecoveryRefused('recovery_service_unavailable') from None
                        time.sleep(0.2)
                yield RecoveryGrant(request_id, OPERATOR, target.endpoint, BANK, self.journal, payload,
                                    digest(payload), True, ROUTES, ROUTES, prove_live, prove_absent_safe,
                                    reservation_root=reservations)

        def locked(root, wait):
            return owner_journal(root, request_id, self.application_user, wait)

        authority = SimpleNamespace(hold=hold)
        return recover(target, OPERATOR, request_id, authority=authority, locked=locked, bank=BANK)


def production_operation():
    return ProtectedRecovery(bind(), state=STATE, service=ROOT / 'recovery',
                             journal=ROOT / 'journal',
                             application_user=pwd.getpwnam('_sios_hindsight'),
                             recovery_user=pwd.getpwnam('_sios_recovery'))
