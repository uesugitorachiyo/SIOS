"""Reconcile the bound Memory tree and index without collecting new content."""
from __future__ import annotations

from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import time

from .basic_memory import BasicMemoryError, _check_node
from .index_binding import load_index
from .pathutils import native_path

MAX_FILES = 10000
MAX_FILE_BYTES = 16_000_000
MAX_TOTAL_BYTES = 256_000_000
DELETED = 'deleted'


def _identity(info):
    identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    # Windows path and open-handle ctime can briefly disagree after a write.
    # File identity, size and modification time remain checked before and after.
    return identity if os.name == 'nt' else (*identity, info.st_ctime_ns)


def _ordinary(info, *, directory):
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValueError('linked Memory entry')
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError('nonordinary Memory entry')


def source_digest(store, relative):
    """Hash one stable, ordinary Memory file; return None only when absent."""
    if not isinstance(relative, str) or not relative.startswith('Memory/') or not relative.lower().endswith('.md'):
        raise ValueError('invalid Memory target')
    store._read_relative(relative)
    store._read_path('Memory')
    path = native_path(store.root)
    for part in relative.split('/'):
        path /= part
        store._check_node(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    _ordinary(before, directory=False)
    if before.st_size > MAX_FILE_BYTES:
        raise ValueError('Memory file exceeds scan limit')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if _identity(before) != _identity(opened):
            raise ValueError('Memory file changed while opening')
        digest = hashlib.sha256()
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        if _identity(opened) != _identity(os.fstat(fd)) or _identity(opened) != _identity(path.lstat()):
            raise ValueError('Memory file changed during scan')
        return digest.hexdigest()
    finally:
        os.close(fd)


def source_snapshot(store):
    root = native_path(store._read_path('Memory'))
    _ordinary(root.lstat(), directory=True)
    sources, directories, total = {}, {}, 0
    def failed(exc):
        raise exc
    for base, dirs, files in os.walk(root, topdown=True, followlinks=False, onerror=failed):
        folder = Path(base)
        current = folder.lstat()
        _ordinary(current, directory=True)
        directories[folder] = _identity(current)
        for name in dirs:
            child = folder / name
            _ordinary(child.lstat(), directory=True)
            store._read_relative('Memory/' + child.relative_to(root).as_posix())
        for name in files:
            child = folder / name
            info = child.lstat()
            _ordinary(info, directory=False)
            if not name.lower().endswith('.md'):
                continue
            relative = 'Memory/' + child.relative_to(root).as_posix()
            store._read_relative(relative)
            total += info.st_size
            if total > MAX_TOTAL_BYTES or len(sources) >= MAX_FILES:
                raise ValueError('Memory scan exceeds resource limit')
            digest = source_digest(store, relative)
            if digest is None:
                raise ValueError('Memory file disappeared during scan')
            sources[relative] = digest
    for folder, before in directories.items():
        if _identity(folder.lstat()) != before:
            raise ValueError('Memory directory changed during scan')
    return sources


def index_snapshot(store):
    index = load_index(store.root)
    index._validate_configuration()
    database = index.config_dir / 'memory.db'
    _check_node(database)
    if not database.is_file():
        raise BasicMemoryError('index database unavailable')
    try:
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
            db.execute('PRAGMA query_only=ON')
            projects = db.execute('SELECT id,name,path FROM project').fetchall()
            if (len(projects) != 1 or projects[0][1] != index.project or
                    Path(projects[0][2]).resolve() != index.memory_root.resolve()):
                raise BasicMemoryError('index project does not match binding')
            rows = db.execute('SELECT file_path,checksum,content_type FROM entity WHERE project_id=? LIMIT ?',
                              (projects[0][0], MAX_FILES + 1)).fetchall()
    except sqlite3.Error:
        raise BasicMemoryError('index snapshot unavailable') from None
    if len(rows) > MAX_FILES:
        raise BasicMemoryError('index snapshot exceeds resource limit')
    result = {}
    for raw, checksum, content_type in rows:
        if (not isinstance(raw, str) or not isinstance(checksum, str) or len(checksum) != 64 or
                any(c not in '0123456789abcdef' for c in checksum) or content_type != 'text/markdown'):
            raise BasicMemoryError('invalid index entity')
        relative = store._read_relative('Memory/' + raw)
        if not relative.lower().endswith('.md') or relative in result:
            raise BasicMemoryError('invalid index path')
        result[relative] = checksum
    return result


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS index_reconcile_status ('
               'id INTEGER PRIMARY KEY CHECK(id=1), status TEXT NOT NULL, checked_at REAL, '
               'differences INTEGER NOT NULL DEFAULT 0)')
    db.execute("INSERT OR IGNORE INTO index_reconcile_status(id,status) VALUES(1,'pending')")


def status(store, state, differences=0):
    with store._connection() as db:
        initialize(db)
        db.execute('UPDATE index_reconcile_status SET status=?,checked_at=?,differences=? WHERE id=1',
                   (state, time.time(), differences))


def reconcile(store):
    """Queue stable changes; never mutate source files or report source content."""
    from .index_retry import initialize as initialize_retry
    try:
        with store._connection() as db:
            store._recover(db)
        indexed = index_snapshot(store)
        sources = source_snapshot(store)
        removed = indexed.keys() - sources.keys()
        if len(removed) > max(10, len(indexed) // 10):
            raise ValueError('too many missing indexed files')
        changes = {path: revision for path, revision in sources.items() if indexed.get(path) != revision}
        changes.update({path: DELETED for path in removed})
        with store._connection() as db:
            initialize_retry(db)
            initialize(db)
            for path, revision in changes.items():
                db.execute('INSERT OR IGNORE INTO index_retry_jobs(path,revision,attempts,next_at,last_error) '
                           'VALUES(?,?,0,?,NULL)', (path, revision, time.time()))
            db.execute("UPDATE index_reconcile_status SET status='ok',checked_at=?,differences=? WHERE id=1",
                       (time.time(), len(changes)))
        return len(changes)
    except Exception:
        status(store, 'unavailable')
        return None
