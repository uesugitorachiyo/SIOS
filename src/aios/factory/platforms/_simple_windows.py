"""Translate the simple check contract to the native Windows Job supervisor."""
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path

from ..contract import Blocked, plain, require
from ._windows_native import RunLock, Supervisor as NativeSupervisor


def _epoch_ns(value):
    instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
    delta = instant - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return ((delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds) * 1000


class Supervisor:
    def __init__(self, run, *, anchor, productive, hard, owner_utc):
        self.run = Path(run)
        self.system_root = os.environ.get('SystemRoot')
        require(type(self.system_root) is str and Path(self.system_root).is_absolute(), 'windows_system_root')
        plain(Path(self.system_root), directory=True)
        self.system_path = plain(Path(self.system_root) / 'System32', directory=True)
        require(self.system_path.is_dir(), 'windows_system_path')
        reference = hashlib.sha256((self.run / 'CHECK_STARTED.json').read_bytes()).hexdigest()
        self.native = NativeSupervisor(
            self.run, productive_allowance_ns=int((productive - anchor) * 1000000000),
            cleanup_allowance_ns=10000000000, parent_hard_deadline_ns=int(hard * 1000000000),
            cap_reference_sha256=reference, productive_deadline_ns=int(productive * 1000000000),
            owner_hard_stop_epoch_ns=_epoch_ns(owner_utc), poll=.05)
        self.records = self.native.records

    @property
    def unknown(self):
        return self.native.unknown

    def git_environment(self):
        from .git_capture import git_environment
        return self._environment(git_environment())

    def _environment(self, env):
        result = dict(env)
        result.update({'SystemRoot': self.system_root, 'PATH': str(self.system_path), 'HOME': str(self.run / 'home'),
                       'TEMP': str(self.run / 'tmp'), 'TMP': str(self.run / 'tmp')})
        result.pop('TMPDIR', None)
        return result

    def call(self, argv, *, cwd, env, stdin=None, success=True, stage='git'):
        require(stdin is None or isinstance(stdin, bytes), 'windows_stdin_type')
        return self.native.call(argv, cwd=cwd, env=self._environment(env),
                                input_bytes=stdin, success=success, role=stage)
