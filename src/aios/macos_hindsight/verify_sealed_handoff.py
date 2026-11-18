"""Read-only host witness required before the protected writer can open."""

import hashlib
import json
import os
import re
import stat
import subprocess


ARTIFACTS = {'database.dump', 'legacy.dump', 'primary.sqlite', 'migration.sqlite'}
SAFE_ENV = {'LC_ALL': 'C', 'PATH': '/usr/bin:/bin:/usr/sbin'}


def _protected_path(path):
    if not path.is_absolute() or '..' in path.parts:
        raise PermissionError('expected an absolute handoff path')
    for directory in (*reversed(path.parents), path):
        attributes = directory.lstat()
        if not stat.S_ISDIR(attributes.st_mode) or attributes.st_uid != 0 or attributes.st_mode & 0o022:
            raise PermissionError('handoff parent is not protected')
    result = subprocess.run(['/bin/ls', '-lde', *map(str, path.parents), str(path)],
                            capture_output=True, check=True, text=True, timeout=5, env=SAFE_ENV)
    if re.search(r'^\s*\d+:\s', result.stdout, re.MULTILINE):
        raise PermissionError('handoff parent has an extended ACL')


def _private_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    stream = os.fdopen(fd, 'rb')
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)):
            raise PermissionError('handoff file is not private administrator state')
        return stream
    except BaseException:
        stream.close()
        raise


def _digest(path):
    result = hashlib.sha256()
    with _private_file(path) as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def _missing(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    return False


def _probe(command):
    return subprocess.run(command, capture_output=True, text=True, timeout=20, env=SAFE_ENV)


def verify_retired_routes(legacy_uid, labels, ports):
    if (type(legacy_uid) is not int or legacy_uid <= 0 or not isinstance(labels, list) or not labels
            or len(labels) > 64 or len(set(labels)) != len(labels)
            or any(not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name)
                   for name in labels)
            or not isinstance(ports, list) or not ports or len(ports) > 64
            or any(type(port) is not int or not 1 <= port <= 65535 for port in ports)
            or len(set(ports)) != len(ports)):
        raise ValueError('a complete administrator-reviewed retired route inventory is required')
    for domain in ('gui', 'user'):
        for name in labels:
            observed = _probe(['/bin/launchctl', 'print', f'{domain}/{legacy_uid}/{name}'])
            if observed.returncode != 113 or not re.search(
                r'Could not find (?:service|domain)', observed.stderr
            ):
                raise PermissionError('a retired launch job is present or its state is unknown')
    for port in ports:
        listener = _probe(['/usr/sbin/lsof', '-nP', '-a', f'-iTCP:{port}', '-sTCP:LISTEN', '-Fp'])
        if listener.returncode != 1 or listener.stdout or listener.stderr:
            raise PermissionError('a retired Hindsight or PostgreSQL TCP listener could not be excluded')


def verify_sealed_handoff(*, handoff, receipt_sha256, source_id, target_id,
                          original_pgdata, retired_pgdata, original_inode, original_journals,
                          original_journal_inode, legacy_uid, legacy_labels, legacy_ports, legacy_socket_port):
    """Verify the archived PostgreSQL and joint-journal handoff before admission.

    The administrator pins all arguments in the protected cutover configuration.
    The old data tree is held inside a root-only vault. The replacement target
    accepts its named operating-system peers over a separate Unix socket.
    """
    if os.geteuid() != 0 or not isinstance(legacy_uid, int) or legacy_uid <= 0:
        raise PermissionError('trusted administrator scope is required')
    if source_id == target_id or not all(
        isinstance(key, str) and re.fullmatch(r'[0-9]{1,20}', key) for key in (source_id, target_id)
    ):
        raise ValueError('the protected and retired cluster identities must differ')
    if not original_pgdata.is_absolute() or not _missing(original_pgdata):
        raise PermissionError('the old data directory is still reachable at its original path')
    if type(legacy_socket_port) is not int or legacy_socket_port not in legacy_ports:
        raise ValueError('the retired PostgreSQL socket must be included in the route inventory')
    if not _missing(original_pgdata.parent / f'.s.PGSQL.{legacy_socket_port}'):
        raise PermissionError('the retired PostgreSQL Unix socket still exists')
    _protected_path(retired_pgdata.parent)
    vault = retired_pgdata.parent.lstat()
    if stat.S_IMODE(vault.st_mode) != 0o700:
        raise PermissionError('the retired data vault is not root-only')
    retired = retired_pgdata.lstat()
    if not stat.S_ISDIR(retired.st_mode) or (retired.st_dev, retired.st_ino) != original_inode:
        raise PermissionError('the sealed source is not the original retired data directory')
    if not original_journals.is_absolute() or not _missing(original_journals):
        raise PermissionError('the original journals are still reachable via the retired path')
    journals = (retired_pgdata.parent / 'journals').lstat()
    if (not stat.S_ISDIR(journals.st_mode) or
            (journals.st_dev, journals.st_ino) != original_journal_inode):
        raise PermissionError('the sealed journals do not match the original retired directory')

    verify_retired_routes(legacy_uid, legacy_labels, legacy_ports)

    _protected_path(handoff)
    if stat.S_IMODE(handoff.lstat().st_mode) != 0o700:
        raise PermissionError('the archive and journal handoff directory is not root-only')
    with _private_file(handoff / 'verified.json') as stream:
        raw = stream.read(8193)
    if len(raw) > 8192 or hashlib.sha256(raw).hexdigest() != receipt_sha256:
        raise ValueError('the approved handoff receipt changed')
    receipt = json.loads(raw)
    if (receipt.get('state') != 'verified_for_review'
            or receipt.get('source_id') != source_id or receipt.get('target_id') != target_id):
        raise ValueError('the handoff receipt does not identify the two clusters')
    digests = receipt.get('file_sha256', {})
    if not isinstance(digests, dict) or set(digests) != ARTIFACTS:
        raise ValueError('both PostgreSQL archives and both journals are required')
    if any(_digest(handoff / name) != digests[name] for name in sorted(ARTIFACTS)):
        raise ValueError('a sealed handoff artifact has changed')
