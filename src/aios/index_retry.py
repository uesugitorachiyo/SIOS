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
import uuid

_ENABLED = ContextVar('sios_index_write_enabled', default=False)


def begin(scopes):
    return _ENABLED.set('read' in scopes and bool({'memory:write', 'memory:index_refresh'} & set(scopes)))


def end(token):
    _ENABLED.reset(token)


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS index_retry_jobs ('
               'path TEXT PRIMARY KEY, revision TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, '
               'next_at REAL NOT NULL, last_error TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS index_repair_uncertainty ('
               'path TEXT PRIMARY KEY, revision TEXT NOT NULL, token TEXT NOT NULL)')


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
        db.execute('DELETE FROM index_retry_jobs WHERE path=? AND revision=? AND NOT EXISTS '
                   '(SELECT 1 FROM index_repair_uncertainty AS u WHERE u.path=index_retry_jobs.path)',
                   (path, revision))


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
        return db.execute('SELECT j.path,j.revision,j.attempts FROM index_retry_jobs AS j '
                          'WHERE j.next_at<=? AND NOT EXISTS '
                          '(SELECT 1 FROM index_repair_uncertainty AS u WHERE u.path=j.path) '
                          'ORDER BY j.next_at,j.path LIMIT 1', (now,)).fetchone()


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


def _mark_repair_unknown(store, path, revision):
    """Persist uncertainty before invoking a repair with possible side effects."""
    # Generic callbacks do not prove they enforce the no-fork protocol.
    # Such markers must not be automatically reconciled by no-fork logic.
    token = 'unknown1-' + uuid.uuid4().hex
    with store._connection() as db:
        initialize(db)
        inserted = db.execute('INSERT OR IGNORE INTO index_repair_uncertainty(path,revision,token) '
                              'SELECT path,revision,? FROM index_retry_jobs WHERE path=? AND revision=?',
                              (token, path, revision)).rowcount
        return token if inserted == 1 else None


def _repair_uncertain(store, path, revision):
    with store._connection() as db:
        initialize(db)
        row = db.execute('SELECT token FROM index_repair_uncertainty WHERE path=?', (path,)).fetchone()
    return row is not None


def _release_repair(store, path, token):
    with store._connection() as db:
        initialize(db)
        return db.execute('DELETE FROM index_repair_uncertainty WHERE path=? AND token=?',
                          (path, token)).rowcount == 1


def retry_one(store, *, now=None, repair=None, stop_event=None):
    """Try one due record; return whether a job was found, never its contents."""
    now = time.time() if now is None else now
    job = _due(store, now)
    if job is None:
        return False
    path, revision, attempts = job
    from .index_reconcile import reconcile
    if reconcile(store) is None:
        if not _repair_uncertain(store, path, revision):
            _defer(store, path, revision, attempts, time.time())
        return True
    try:
        with store._connection() as db:
            store._recover(db)
        from .index_reconcile import DELETED, source_digest
        if (not isinstance(path, str) or not path.startswith('Memory/') or not path.lower().endswith('.md')
                or not isinstance(revision, str) or (revision != DELETED and re.fullmatch(r'[0-9a-f]{64}', revision) is None)):
            raise ValueError('invalid queued target')
        actual = source_digest(store, path) or DELETED
        if actual != revision:
            # A repair with unknown outcome might still be running. Do not
            # discard its durable marker just because the source has changed.
            if not _repair_uncertain(store, path, revision):
                _retarget(store, path, revision, actual, now)
            return True
        from .index_binding import load_index
        index = load_index(store.root)
        ready = asyncio.run(index.wait_for_files({path.removeprefix('Memory/'): None if revision == DELETED else revision}, timeout=7))
        # A later observation may resolve an earlier unknown outcome, but it
        # must never launch a second repair while that outcome is unknown.
        if _repair_uncertain(store, path, revision):
            return True
        if not ready and repair is not None and revision != DELETED:
            if stop_event is not None and stop_event.is_set():
                _defer(store, path, revision, attempts, time.time())
                return True
            token = _mark_repair_unknown(store, path, revision)
            if token:
                try:
                    outcome = repair(index, rel_path=path.removeprefix('Memory/'), expected_revision=revision,
                                     store=store, marker_token=token)
                except Exception:
                    return True
                if not isinstance(outcome,dict) or outcome.get('settled') is not True:
                    return True
                settled_token=outcome.get('marker_token') or token
                if not _release_repair(store, path, settled_token):
                    return True
                if (source_digest(store, path) or DELETED) != revision:
                    return True
                ready = asyncio.run(index.wait_for_files({path.removeprefix('Memory/'): revision}, timeout=7))
        if ready and (source_digest(store, path) or DELETED) == revision:
            complete(store, path, revision)
        elif not _repair_uncertain(store, path, revision):
            _defer(store, path, revision, attempts, time.time())
    except Exception:
        if not _repair_uncertain(store, path, revision):
            _defer(store, path, revision, attempts, time.time())
    return True


class RetryWorker:
    """One service-owned worker with persistent jobs and bounded backoff."""
    def __init__(self, store, *, scan_interval=60, poll_interval=2, repair=None, resume_uncertain=False):
        if not 0 < scan_interval <= 3600 or not 0 < poll_interval <= 60:
            raise ValueError('invalid reconciliation interval')
        self.store = store
        self.scan_interval = scan_interval
        self.poll_interval = poll_interval
        self.repair = repair
        self.resume_uncertain = resume_uncertain
        self._resume_cursor = ''
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name='sios-index-retry', daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _run(self):
        next_scan = 0.0
        while not self.stopping.wait(self.poll_interval):
            try:
                now = time.monotonic()
                if now >= next_scan:
                    from .index_reconcile import reconcile
                    reconcile(self.store)
                    if self.resume_uncertain:
                        from .index_repair_resume import reconcile_one
                        with self.store._connection() as db:
                            initialize(db)
                            paths=[row[0] for row in db.execute(
                                'SELECT path FROM index_repair_uncertainty WHERE path>? ORDER BY path LIMIT 32',
                                (self._resume_cursor,)).fetchall()]
                        self._resume_cursor = paths[-1] if paths else ''
                        for path in paths:
                            if self.stopping.is_set(): break
                            reconcile_one(self.store,path)
                    next_scan = time.monotonic() + self.scan_interval
                retry_one(self.store,repair=self.repair,stop_event=self.stopping)
            except Exception:
                # Storage can recover later; keep the service alive without
                # logging paths, source content or private exceptions.
                continue

    def stop(self, timeout=10):
        self.stopping.set()
        self.thread.join(timeout=timeout)
        return not self.thread.is_alive()
