"""Use the existing journal locks and SQLite owner for administrative recovery."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import time

from ..hindsight_journal import PrimaryJournal, _private_file


REQUEST = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{7,127}\Z')
MAX_RESPONSE = 65536


def _write(value):
    data = json.dumps(value, separators=(',', ':'), ensure_ascii=True).encode('ascii') + b'\n'
    if len(data) > MAX_RESPONSE:
        raise ValueError('recovery journal response exceeds its size limit')
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def worker(root, request_id):
    if not os.getuid() or os.geteuid() != os.getuid() or not REQUEST.fullmatch(request_id):
        raise PermissionError('expected an unprivileged journal owner')
    # Refuse a missing handoff journal rather than initializing an empty store.
    for name in ('primary-target.lock', 'primary.lock', 'primary.sqlite'):
        _private_file(root / name)
    with PrimaryJournal(root, target_lock=root / 'primary-target.lock') as journal:
        _write({'ready': True})
        while command := sys.stdin.buffer.readline(33):
            if command == b'get\n':
                _write({'row': journal.get(request_id)})
            elif command == b'commit\n':
                journal.mark_committed(request_id)
                _write({'committed': True})
            else:
                raise ValueError('unknown recovery journal command')


class _Journal:
    def __init__(self, child, request_id, wait):
        self.child = child
        self.request_id = request_id
        self.wait = wait

    def receive(self):
        fd = self.child.stdout.fileno()
        deadline = time.monotonic() + self.wait
        data = bytearray()
        while len(data) < MAX_RESPONSE:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise TimeoutError('the journal owner did not respond')
            block = os.read(fd, min(4096, MAX_RESPONSE - len(data)))
            if not block:
                raise RuntimeError('the journal owner exited')
            data.extend(block)
            if data.endswith(b'\n'):
                return json.loads(data)
        raise ValueError('recovery journal response exceeds its size limit')

    def _command(self, code, request_id):
        if request_id != self.request_id:
            raise PermissionError('recovery journal request binding changed')
        self.child.stdin.write(code + b'\n')
        self.child.stdin.flush()
        return self.receive()

    def get(self, request_id):
        return self._command(b'get', request_id)['row']

    def mark_committed(self, request_id):
        if self._command(b'commit', request_id) != {'committed': True}:
            raise RuntimeError('journal commitment did not verify')


@contextmanager
def owner_journal(root, request_id, owner, wait):
    if os.getuid() != 0 or os.geteuid() != 0 or not owner.pw_uid:
        raise PermissionError('administrator delegation to a journal owner is required')
    if not REQUEST.fullmatch(request_id):
        raise ValueError('invalid recovery request id')
    code = (
        'import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);'
        'from aios.macos_hindsight.protected_recovery_journal import worker;worker(Path(sys.argv[2]),sys.argv[3])'
    )
    child = subprocess.Popen(
        ['/usr/bin/python3', '-I', '-B', '-c', code, str(Path(__file__).resolve().parents[2]), str(root), request_id],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        user=owner.pw_uid, group=owner.pw_gid, extra_groups=[], cwd='/private/tmp', umask=0o077,
        env={'PATH': '/usr/bin:/bin', 'HOME': '/var/empty', 'LC_ALL': 'C'},
    )
    try:
        journal = _Journal(child, request_id, max(2, min(wait, 15)))
        if journal.receive() != {'ready': True}:
            raise RuntimeError('the journal owner did not acquire its locks')
        yield journal
    finally:
        child.stdin.close()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()
