"""Bind trusted writer control to the administrator-installed service definitions."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import time

from .pg_wire_candidate import PgConnection, PgError
from .protected_launchd import MacWriterServices, ManualJob
from .protected_writer_supervisor import WriterSupervisor, administrator_lock
from .protected_writer_transition import BANK_DATABASE, HOST_ADMIN
from .verify_sealed_handoff import _private_file, _protected_path, verify_sealed_handoff


ROOT = Path('/Library/Application Support/SIOS-Hindsight-Protected')
STATE = ROOT / 'administrator'
PG_SOCKET = ROOT / 'pg'
JOB_IDS = {
    'database': ('local.sios.hindsight-postgres-protected', '_postgres'),
    'application': ('local.sios.hindsight-application-protected', '_sios_hindsight'),
    'recovery': ('local.sios.hindsight-recovery-protected', '_sios_recovery'),
}


def validate_deployment(config):
    """Reject ambiguous identities or incomplete route inventories before launchd changes."""
    if not isinstance(config, dict) or set(config) != {
        'legacy_uid', 'port', 'source_id', 'target_id', 'receipt_sha256', 'original_pgdata',
        'original_inode', 'original_journals', 'original_journal_inode', 'jobs', 'legacy_labels',
        'legacy_ports', 'legacy_socket_port',
    }:
        raise ValueError('unexpected protected deployment fields')
    if type(config['legacy_uid']) is not int or config['legacy_uid'] <= 0:
        raise ValueError('a non-root legacy identity is required')
    if type(config['port']) is not int or not 1 <= config['port'] <= 65535:
        raise ValueError('unexpected protected PostgreSQL socket port')
    if (not isinstance(config['jobs'], dict) or set(config['jobs']) != set(JOB_IDS)
            or any(not isinstance(pin, str) or not re.fullmatch(r'[a-f0-9]{64}', pin)
                   for pin in config['jobs'].values())
            or not isinstance(config['receipt_sha256'], str)
            or not re.fullmatch(r'[a-f0-9]{64}', config['receipt_sha256'])):
        raise ValueError('the protected jobs and handoff must have complete SHA-256 pins')
    if (not all(isinstance(config[key], str) and re.fullmatch(r'[0-9]{1,20}', config[key])
                for key in ('source_id', 'target_id')) or config['source_id'] == config['target_id']):
        raise ValueError('the source and target cluster identities must differ')
    for key in ('original_pgdata', 'original_journals'):
        if (not isinstance(config[key], str) or not Path(config[key]).is_absolute()
                or '..' in Path(config[key]).parts):
            raise ValueError('the retired directory paths must be absolute')
    if config['original_pgdata'] == config['original_journals']:
        raise ValueError('the retired database and journal must be distinct directories')
    for key in ('original_inode', 'original_journal_inode'):
        value = config[key]
        if (not isinstance(value, list) or len(value) != 2
                or any(type(item) is not int or item < 0 for item in value)):
            raise ValueError('the retired directory identities must contain a device and inode')
    labels, ports = config['legacy_labels'], config['legacy_ports']
    if (not isinstance(labels, list) or not labels or len(labels) > 64
            or any(not isinstance(label, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', label)
                   for label in labels) or len(set(labels)) != len(labels)
            or set(labels) & {label for label, _ in JOB_IDS.values()}):
        raise ValueError('the retired launchd labels must be distinct and exclude protected jobs')
    if (not isinstance(ports, list) or not ports or len(ports) > 64
            or any(type(port) is not int or not 1 <= port <= 65535 for port in ports)
            or len(set(ports)) != len(ports) or config['port'] in ports
            or type(config['legacy_socket_port']) is not int or config['legacy_socket_port'] not in ports):
        raise ValueError('the retired listener inventory must include the old PostgreSQL socket port')
    return config


def bind():
    """Called by the protected local administrator launcher after maintenance handoff."""
    if os.getuid() != 0 or os.geteuid() != 0:
        raise PermissionError('macOS administrator identity required')
    _protected_path(STATE)
    with _private_file(STATE / 'deployment.json') as stream:
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise ValueError('protected deployment configuration exceeds its size limit')
    config = validate_deployment(json.loads(raw))
    port = config['port']
    jobs = {
        mode: ManualJob(label, user, STATE / (label + '.plist'), config['jobs'][mode])
        for mode, (label, user) in JOB_IDS.items()
    }
    for job in jobs.values():
        job.verify_definition()
    services = MacWriterServices(**jobs)
    _protected_path(PG_SOCKET.parent)
    directory = PG_SOCKET.lstat()
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != jobs['database'].uid
            or stat.S_IMODE(directory.st_mode) != 0o750):
        raise PermissionError('the private PostgreSQL socket directory is untrusted')

    @contextmanager
    def connect():
        deadline = time.monotonic() + 12
        while True:
            try:
                socket = (PG_SOCKET / f'.s.PGSQL.{port}').lstat()
                if (not stat.S_ISSOCK(socket.st_mode) or socket.st_uid != jobs['database'].uid
                        or stat.S_IMODE(socket.st_mode) != 0o770):
                    raise ValueError('the PostgreSQL socket ownership or permissions changed')
                connection = PgConnection(user=HOST_ADMIN, database=BANK_DATABASE, port=port,
                                          unix_dir=str(PG_SOCKET), timeout=2)
                break
            except (PgError, OSError):
                if time.monotonic() >= deadline:
                    raise RuntimeError('the protected PostgreSQL endpoint is unavailable') from None
                time.sleep(0.2)
        with connection:
            yield connection

    def verify_cutover():
        verify_sealed_handoff(handoff=STATE / 'handoff', receipt_sha256=config['receipt_sha256'],
                              source_id=config['source_id'], target_id=config['target_id'],
                              original_pgdata=Path(config['original_pgdata']),
                              retired_pgdata=STATE / 'retired' / 'database',
                              original_inode=tuple(config['original_inode']),
                              original_journals=Path(config['original_journals']),
                              original_journal_inode=tuple(config['original_journal_inode']),
                              legacy_uid=config['legacy_uid'], legacy_labels=config['legacy_labels'],
                              legacy_ports=config['legacy_ports'], legacy_socket_port=config['legacy_socket_port'])

    return WriterSupervisor(connect=connect, cluster=config['target_id'], services=services,
                            verify_cutover=verify_cutover,
                            lock=lambda: administrator_lock(STATE / 'transition.lock'))
