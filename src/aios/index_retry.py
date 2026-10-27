"""Durable, bounded retry of index work for authorized Memory writes.

This queue contains paths and revisions only; it does not collect conversations
or write source notes. Its lifetime is the central service's lifetime.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import hashlib
import re
import threading
import time

_ENABLED = ContextVar('sios_index_write_enabled', default=False)


def begin(scopes):
    return _ENABLED.set('read' in scopes and bool({'memory:write', 'memory:index_refresh'} & set(scopes)))


def end(token):
    _ENABLED.reset(token)


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS index_retry_jobs ('
               'path TEXT PRIMARY KEY, revision TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, '
               'next_at REAL NOT NULL, last_error TEXT)')


def enqueue_intents(db, intent):
    if not _ENABLED.get():
        return
    initialize(db)
    for item in intent:
        path, content = item['path'], item['text']
        if not path.startswith('Memory/') or not path.endswith('.md'):
            continue
        revision = hashlib.sha256(content.encode('utf-8')).hexdigest()
        db.execute('INSERT INTO index_retry_jobs(path,revision,attempts,next_at,last_error) '
                   'VALUES(?,?,0,?,NULL) ON CONFLICT(path) DO UPDATE SET '
                   'revision=excluded.revision, attempts=0, next_at=excluded.next_at, last_error=NULL',
                   (path, revision, time.time() + 10))


def complete(store, path, revision):
    with store._connection() as db:
        initialize(db)
        db.execute('DELETE FROM index_retry_jobs WHERE path=? AND revision=?', (path, revision))


def enqueue_if_absent(store, path, revision):
    """Retain a failed refresh even for a duplicate with no new write intent."""
    if (not isinstance(path, str) or not path.startswith('Memory/') or not path.endswith('.md')
            or not isinstance(revision, str) or re.fullmatch(r'[0-9a-f]{64}', revision) is None):
        return
    store._read_relative(path)
    with store._connection() as db:
        initialize(db)
        db.execute('INSERT OR IGNORE INTO index_retry_jobs(path,revision,attempts,next_at,last_error) '
                   'VALUES(?,?,0,?,?)', (path, revision, time.time() + 10, 'not_confirmed'))


def pending_count(store):
    with store._connection() as db:
        initialize(db)
        return db.execute('SELECT count(*) FROM index_retry_jobs').fetchone()[0]


def _due(store, now):
    with store._connection() as db:
        initialize(db)
        return db.execute('SELECT path,revision,attempts FROM index_retry_jobs '
                          'WHERE next_at<=? ORDER BY next_at,path LIMIT 1', (now,)).fetchone()


def _defer(store, path, revision, attempts, now):
    delay = min(300, 5 * (2 ** min(attempts, 6)))
    with store._connection() as db:
        initialize(db)
        db.execute('UPDATE index_retry_jobs SET attempts=attempts+1,next_at=?,last_error=? '
                   'WHERE path=? AND revision=?', (now + delay, 'not_confirmed', path, revision))


def _retarget(store, path, old_revision, new_revision, now):
    with store._connection() as db:
        initialize(db)
        db.execute('UPDATE index_retry_jobs SET revision=?,attempts=0,next_at=?,last_error=NULL '
                   'WHERE path=? AND revision=?', (new_revision, now, path, old_revision))


def retry_one(store, *, now=None):
    """Try one due record; return whether a job was found, never its contents."""
    now = time.time() if now is None else now
    job = _due(store, now)
    if job is None:
        return False
    path, revision, attempts = job
    try:
        if (not isinstance(path, str) or not path.startswith('Memory/') or not path.endswith('.md')
                or not isinstance(revision, str) or re.fullmatch(r'[0-9a-f]{64}', revision) is None):
            raise ValueError('invalid queued target')
        current = store.read(path)
        actual = current['revision']
        if actual != revision:
            _retarget(store, path, revision, actual, now)
            return True
        from .index_binding import load_index
        index = load_index(store.root)
        ready = asyncio.run(index.wait_for_files({path.removeprefix('Memory/'): revision}, timeout=7))
        if ready and store.read(path)['revision'] == revision:
            complete(store, path, revision)
        else:
            _defer(store, path, revision, attempts, time.time())
    except Exception:
        _defer(store, path, revision, attempts, time.time())
    return True


class RetryWorker:
    """One service-owned worker with persistent jobs and bounded backoff."""
    def __init__(self, store):
        self.store = store
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name='sios-index-retry', daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _run(self):
        while not self.stopping.wait(2):
            try:
                retry_one(self.store)
            except Exception:
                # Storage can recover later; keep the service alive without
                # logging paths, source content or private exceptions.
                continue

    def stop(self):
        self.stopping.set()
        self.thread.join(timeout=10)
