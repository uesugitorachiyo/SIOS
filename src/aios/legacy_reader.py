"""POSIX read-only bridge to an existing owner-controlled knowledge tree.

Never initializes storage, creates locks, opens SQLite, or recovers transactions.
Reads cooperate with the existing flock writer. Direct edits and hostile path
replacement are outside this cooperative boundary. Reading may update atimes.
"""
from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import os
from pathlib import Path
import stat

from .store import ValidationError

MAX_BYTES = 2_000_000
MAX_FILES = 5_000
MAX_SEARCH_BYTES = 32_000_000
MAX_RESULTS = 1_000


class LegacyReader:
    def __init__(self, data_root: Path):
        if os.name != 'posix':
            raise ValidationError('Legacy reader requires a POSIX authority host')
        # Localized dependency: ordinary cross-platform software never imports it.
        import fcntl
        self._fcntl = fcntl
        self.root = Path(os.path.abspath(data_root))
        self._path('')
        if not self.root.is_dir():
            raise ValidationError('Legacy root must already exist')
        with self._locked():
            pass

    @staticmethod
    def _check(path):
        info = path.lstat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValidationError('Legacy paths must be ordinary files or directories')
        if getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValidationError('Reparse points are forbidden')

    def _path(self, relative):
        if not isinstance(relative, str) or '\\' in relative or '\x00' in relative or ':' in relative:
            raise ValidationError('Invalid legacy path')
        parts = relative.split('/') if relative else []
        if any(part in ('', '.', '..') for part in parts):
            raise ValidationError('Invalid legacy path')
        for node in (*reversed(self.root.parents), self.root):
            self._check(node)
        current = self.root
        for part in parts:
            current /= part
            self._check(current)
        return current

    @contextlib.contextmanager
    def _locked(self):
        path = self._path('Runs/.state.lock')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValidationError('Legacy lock must be a regular file')
            self._fcntl.flock(fd, self._fcntl.LOCK_SH)
            yield
        finally:
            os.close(fd)

    def _bytes(self, relative):
        path = self._path(relative)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValidationError('Expected a regular legacy file')
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValidationError('Legacy file exceeds size limit')
        return data

    @staticmethod
    def _readable(relative):
        if not isinstance(relative, str):
            raise ValidationError('Read path must be a string')
        parts = relative.split('/')
        if any(part.startswith('.') for part in parts):
            raise ValidationError('Hidden paths are not exposed')
        knowledge = (relative.startswith(('Brain/brain/', 'Memory/', 'Missions/'))
                     and relative.endswith('.md'))
        run = (len(parts) == 3 and parts[0] == 'Runs'
               and relative.endswith(('.md', '.json')))
        if not (knowledge or run):
            raise ValidationError('Path is outside exposed legacy knowledge')

    def read(self, relative):
        self._readable(relative)
        with self._locked():
            data = self._bytes(relative)
            return {'path': relative, 'revision': hashlib.sha256(data).hexdigest(),
                    'text': data.decode('utf-8')}

    def _files(self, relative, suffix):
        base = self._path(relative)
        if not base.is_dir():
            raise ValidationError('Expected a legacy directory')
        count = 0
        def scan_error(error):
            raise error
        for directory, dirs, files in os.walk(base, followlinks=False, onerror=scan_error):
            # Validate every traversed node, including ancestors of the base.
            dirs.sort()
            for name in dirs + files:
                self._path((Path(directory) / name).relative_to(self.root).as_posix())
                count += 1
                if count > MAX_FILES:
                    raise ValidationError('Legacy scan exceeds entry limit')
            dirs[:] = [name for name in dirs if not name.startswith('.')]
            for name in sorted(files):
                if not name.startswith('.') and name.endswith(suffix):
                    yield (Path(directory) / name).relative_to(self.root).as_posix()

    def search(self, store, query):
        roots = {'brain': 'Brain/brain', 'memory': 'Memory', 'missions': 'Missions'}
        if not isinstance(store, str) or store not in roots:
            raise ValidationError('Unknown store')
        if not isinstance(query, str) or not query.strip() or '\x00' in query or len(query.encode('utf-8')) > 1000:
            raise ValidationError('Invalid query')
        with self._locked():
            results, total = [], 0
            for relative in self._files(roots[store], '.md'):
                data = self._bytes(relative)
                total += len(data)
                if total > MAX_SEARCH_BYTES:
                    raise ValidationError('Legacy search exceeds byte limit')
                text = data.decode('utf-8')
                index = text.casefold().find(query.casefold())
                if index >= 0:
                    results.append({'path': relative, 'revision': hashlib.sha256(data).hexdigest(),
                                    'excerpt': text[max(0, index - 80):index + 240]})
                    if len(results) > MAX_RESULTS:
                        raise ValidationError('Legacy search exceeds result limit')
            return sorted(results, key=lambda result: result['path'])

    def history(self, routine):
        from .identifiers import portable_component
        try:
            if portable_component(routine) != routine:
                raise ValueError()
        except (TypeError, ValueError) as exc:
            raise ValidationError('Invalid routine') from exc
        with self._locked():
            runs, total = [], 0
            for relative in self._files('Runs/' + routine, '.json'):
                self._readable(relative)
                data = self._bytes(relative)
                total += len(data)
                if total > MAX_SEARCH_BYTES:
                    raise ValidationError('Legacy history exceeds byte limit')
                value = json.loads(data)
                if not isinstance(value, dict):
                    raise ValidationError('Invalid legacy run metadata')
                if value.get('publication') == 'complete':
                    items = value.get('items')
                    if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
                        raise ValidationError('Invalid legacy run items')
                    runs.append(value)
                    if len(runs) > MAX_RESULTS:
                        raise ValidationError('Legacy history exceeds result limit')
            return {'routine': routine, 'runs': runs,
                    'items': sorted({item for run in runs for item in run['items']})}

    def health(self):
        with self._locked():
            return {'ok': True, 'read_only': True, 'backend': 'legacy-posix'}


def legacy_dispatch(store, operation, params, *, actor, scopes):
    if not isinstance(operation, str) or operation not in {'read', 'search', 'health', 'history'}:
        raise PermissionError('Operation is unavailable on the read-only legacy gateway')
    if 'read' not in scopes:
        raise PermissionError('Read scope is required')
    if not isinstance(params, dict) or {'actor', 'owner_authorized'} & set(params):
        raise ValidationError('Invalid operation parameters')
    method = getattr(store, operation)
    try:
        inspect.signature(method).bind(**params)
    except TypeError as exc:
        raise ValidationError('Invalid operation parameters') from exc
    return method(**params)
