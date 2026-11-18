"""Mac-only protected journal for the uninstalled direct Hindsight writer.

An exclusive OS lock spans the writer call and all journal transitions. The
journal stores request metadata and hashes, never content or credentials.
This candidate does not authorize, start or register a service.
"""
from __future__ import annotations
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
from datetime import datetime, timezone


def _private_dir(path):
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:
        raise ValueError('invalid_private_directory')


def _private_file(path):
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1:
        raise ValueError('invalid_private_file')


class PrimaryJournal:
    def __init__(self, root, *, target_lock):
        self.root=Path(root)
        self.target_lock=Path(target_lock)
        self._target_fd=None
        self._fd=None
        self._db=None

    def __enter__(self):
        _private_dir(self.root)
        # The host must supply the exact lock used by all cooperative writers
        # to this target. This does not fence unrelated/noncooperating clients.
        self._target_fd=os.open(self.target_lock,os.O_RDWR|getattr(os,'O_NOFOLLOW',0))
        info=os.fstat(self._target_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid()
                or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1):
            os.close(self._target_fd);self._target_fd=None
            raise ValueError('invalid_target_lock')
        try:
            fcntl.flock(self._target_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BaseException:
            os.close(self._target_fd);self._target_fd=None
            raise
        lock=self.root/'primary.lock'
        try:
            self._fd=os.open(lock,os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
            _private_file(lock)
            fcntl.flock(self._fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            path=self.root/'primary.sqlite'
            if not path.exists():
                fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|getattr(os,'O_NOFOLLOW',0),0o600)
                os.close(fd)
                directory_fd=os.open(self.root,os.O_RDONLY)
                try:os.fsync(directory_fd)
                finally:os.close(directory_fd)
            _private_file(path)
            self._db=sqlite3.connect(path,timeout=5,isolation_level=None)
            self._db.execute('PRAGMA synchronous=FULL')
            self._db.execute('CREATE TABLE IF NOT EXISTS writes (request_id TEXT PRIMARY KEY,document_id TEXT NOT NULL UNIQUE,semantic_id TEXT UNIQUE,metadata TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN (\'unknown\',\'committed\')),updated_at TEXT NOT NULL)')
            # Initialize the logical-note tables even when this process only
            # captures immutable notes, so read-only search can distinguish an
            # empty journal from an unavailable or incompatible journal.
            self._db.execute('CREATE TABLE IF NOT EXISTS note_writes (request_id TEXT PRIMARY KEY, identity TEXT NOT NULL, path TEXT NOT NULL, document_id TEXT NOT NULL, revision TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN (\'unknown\',\'committed\')), actor_hash TEXT)')
            self._db.execute('CREATE UNIQUE INDEX IF NOT EXISTS note_writes_pending_path ON note_writes(path) WHERE state=\'unknown\'')
            self._db.execute('CREATE TABLE IF NOT EXISTS note_heads (path TEXT PRIMARY KEY, document_id TEXT NOT NULL, revision TEXT NOT NULL)')
            return self
        except BaseException:
            if self._db is not None:self._db.close();self._db=None
            if self._fd is not None:os.close(self._fd);self._fd=None
            os.close(self._target_fd);self._target_fd=None
            raise

    def __exit__(self,*exc):
        if self._db is not None:self._db.close();self._db=None
        if self._fd is not None:os.close(self._fd);self._fd=None
        if self._target_fd is not None:os.close(self._target_fd);self._target_fd=None

    def get(self,request_id):
        row=self._db.execute('SELECT document_id,metadata,state FROM writes WHERE request_id=?',(request_id,)).fetchone()
        if row is None:return None
        return {'request_id':request_id,'document_id':row[0],'metadata':json.loads(row[1]),'state':row[2]}

    def get_semantic(self,semantic_id):
        row=self._db.execute('SELECT request_id FROM writes WHERE semantic_id=?',(semantic_id,)).fetchone()
        return self.get(row[0]) if row else None

    def primary_successors(self,document_id):
        rows=self._db.execute("SELECT request_id FROM writes WHERE state='committed' AND json_extract(metadata, '$.supersedes_document_id')=? ORDER BY request_id LIMIT 101",(document_id,)).fetchall()
        return [self.get(row[0]) for row in rows]

    def primary_pending_or_committed_successors(self,document_id):
        rows=self._db.execute("SELECT request_id FROM writes WHERE state IN ('unknown','committed') AND json_extract(metadata, '$.supersedes_document_id')=? ORDER BY request_id LIMIT 101",(document_id,)).fetchall()
        return [self.get(row[0]) for row in rows]

    def insert_unknown(self,row):
        if not isinstance(row,dict) or row.get('state')!='unknown':raise ValueError('invalid_journal_row')
        raw=json.dumps(row['metadata'],ensure_ascii=False,sort_keys=True,separators=(',',':'))
        self._db.execute('INSERT INTO writes VALUES(?,?,?,?,?,?)',(row['request_id'],row['document_id'],row['metadata'].get('semantic_id'),raw,'unknown',datetime.now(timezone.utc).isoformat()))

    def mark_committed(self,request_id):
        cur=self._db.execute("UPDATE writes SET state='committed',updated_at=? WHERE request_id=? AND state='unknown'",(datetime.now(timezone.utc).isoformat(),request_id))
        if cur.rowcount!=1:raise ValueError('journal_transition_invalid')


class PrimaryJournalReader:
    """Read committed correction links without taking the writers' lock.

    Missing journal is reported as unavailable, not as proof of no corrections.
    SQLite provides a consistent read of each query; coverage remains unknown.
    """
    def __init__(self, root):
        self.root=Path(root)
        self._db=None

    def __enter__(self):
        _private_dir(self.root)
        path=self.root/'primary.sqlite'
        if not path.exists():return None
        _private_file(path)
        self._db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)
        return self

    def __exit__(self,*exc):
        if self._db is not None:self._db.close();self._db=None

    get=PrimaryJournal.get
    primary_successors=PrimaryJournal.primary_successors
