"""Control pinned, manually started jobs in the macOS system domain."""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import plistlib
import pwd
import re
import stat
import subprocess
import sys
import time


PLIST_KEYS = {
    'Label', 'UserName', 'GroupName', 'ProgramArguments', 'WorkingDirectory',
    'EnvironmentVariables', 'Umask', 'RunAtLoad', 'KeepAlive',
    'StandardOutPath', 'StandardErrorPath',
}


@dataclass(frozen=True)
class ManualJob:
    label: str
    user: str
    definition: Path
    sha256: str

    def __post_init__(self):
        if (not re.fullmatch(r'local\.sios\.[a-z0-9-]+', self.label)
                or not re.fullmatch(r'[a-f0-9]{64}', self.sha256)):
            raise ValueError('expected a pinned SIOS system job')
        if not self.definition.is_absolute() or self.definition.name != self.label + '.plist':
            raise ValueError('unexpected launchd definition path')
        if not self.uid:
            raise ValueError('the service must run unprivileged')

    @property
    def uid(self):
        return pwd.getpwnam(self.user).pw_uid

    @property
    def target(self):
        return 'system/' + self.label

    def verify_definition(self):
        for directory in (*reversed(self.definition.parents),):
            entry = directory.lstat()
            if not stat.S_ISDIR(entry.st_mode) or entry.st_uid != 0 or entry.st_mode & 0o022:
                raise PermissionError('system job definition has a writable parent')
        entry = self.definition.lstat()
        if (not stat.S_ISREG(entry.st_mode) or entry.st_uid != 0 or entry.st_nlink != 1
                or stat.S_IMODE(entry.st_mode) != 0o600):
            raise PermissionError('system job definition is untrusted')
        # Reject extended ACLs, including on the job's parent directories.
        result = subprocess.run(['/bin/ls', '-lde', *map(str, self.definition.parents), str(self.definition)],
                                check=True, capture_output=True, text=True, timeout=5,
                                env={'LC_ALL': 'C'})
        if re.search(r'^\s*\d+:\s', result.stdout, re.MULTILINE):
            raise PermissionError('system job definition has an extended ACL')
        data = self.definition.read_bytes()
        if hashlib.sha256(data).hexdigest() != self.sha256:
            raise ValueError('system job definition changed')
        info = plistlib.loads(data)
        arguments = info.get('ProgramArguments', [])
        if (set(info) - PLIST_KEYS or info.get('Label') != self.label or info.get('UserName') != self.user
                or info.get('RunAtLoad') is not False or info.get('KeepAlive') is not False
                or not arguments or not Path(arguments[0]).is_absolute()):
            raise ValueError('system job can start automatically or has an unexpected identity')


class MacWriterServices:
    def __init__(self, *, database, application, recovery):
        if sys.platform != 'darwin' or os.geteuid() != 0:
            raise PermissionError('the macOS administrator must control system jobs')
        self.jobs = {'database': database, 'application': application, 'recovery': recovery}
        if len({job.uid for job in self.jobs.values()}) != 3 or len({job.label for job in self.jobs.values()}) != 3:
            raise ValueError('each protected job requires its own account and label')

    def _launchctl(self, *arguments, absent_ok=False):
        result = subprocess.run(['/bin/launchctl', *map(str, arguments)], capture_output=True,
                                text=True, timeout=30, env={'LC_ALL': 'C', 'PATH': '/usr/bin:/bin'})
        if result.returncode == 113 and absent_ok and 'Could not find service' in result.stderr:
            return None
        if result.returncode:
            raise RuntimeError(f'launchctl {arguments[0]} failed for the protected job ({result.returncode})')
        return result.stdout

    def _state(self, job):
        return self._launchctl('print', job.target, absent_ok=True)

    def _pid(self, state):
        found = re.search(r'^\s*pid = ([1-9][0-9]*)$', state or '', re.MULTILINE)
        return int(found.group(1)) if found else None

    def _wait(self, predicate, reason):
        end = time.monotonic() + 12
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(0.1)
        raise RuntimeError(reason)

    def _prove_running(self, job):
        state = self._state(job)
        pid = self._pid(state)
        if not pid or not re.search(r'^\s*state = running$', state, re.MULTILINE):
            return False
        observed = subprocess.run(['/bin/ps', '-o', 'uid=', '-p', str(pid)], text=True,
                                  capture_output=True, timeout=5)
        return observed.returncode == 0 and observed.stdout.strip() == str(job.uid)

    def _stop(self, job):
        state = self._state(job)
        if state is None:
            return
        pid = self._pid(state)
        self._launchctl('bootout', job.target)

        def unloaded():
            if self._state(job) is not None:
                return False
            if pid:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return True
                return False
            return True

        self._wait(unloaded, 'protected launchd job did not stop')

    def _start(self, job):
        if self._state(job) is not None:
            raise RuntimeError('protected launchd job is already loaded')
        job.verify_definition()
        self._launchctl('bootstrap', 'system', job.definition)
        self._launchctl('kickstart', job.target)
        self._wait(lambda: self._prove_running(job), 'protected job did not reach its expected OS identity')

    def prove_writers_stopped(self):
        if any(self._state(self.jobs[mode]) is not None for mode in ('application', 'recovery')):
            raise RuntimeError('writer launchd jobs are not both unloaded')

    def stop_writer(self, mode):
        self._stop(self.jobs[mode])

    def start_writer(self, mode):
        self.prove_writers_stopped()
        self._start(self.jobs[mode])

    def prove_only_writer(self, mode):
        other = 'application' if mode == 'recovery' else 'recovery'
        if self._state(self.jobs[other]) is not None or not self._prove_running(self.jobs[mode]):
            raise RuntimeError('exactly one selected writer job must be running')

    def stop_database(self):
        self._stop(self.jobs['database'])

    def restart_database(self):
        self.prove_writers_stopped()
        self.stop_database()
        self._start(self.jobs['database'])
