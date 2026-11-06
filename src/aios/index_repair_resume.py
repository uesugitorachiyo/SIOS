"""Candidate reconciliation for no-fork repair attempts; not registered."""
import fcntl
import os
import re
import stat
from .index_binding import load_index
from .index_retry import initialize


def reconcile_one(store, path):
    """Release only a candidate no-fork marker while holding its child lock.

    Does not itself mark a job complete; normal retry must verify its current
    source and full index. Other generations of markers remain unconfirmed.
    """
    with store._connection() as db:
        initialize(db)
        marker=db.execute('SELECT token FROM index_repair_uncertainty WHERE path=?',(path,)).fetchone()
    if marker is None: return 'no_marker'
    token=marker[0]
    match = re.fullmatch(r'nofork2-([0-9]+)-([0-9]+)-([0-9]+)-([0-9]+)-([0-9a-f]{32})',token) if isinstance(token,str) else None
    if match is None:
        return 'unknown_marker'
    try:
        index=load_index(store.root)
        index._validate_configuration()
        database=(index.config_dir/'memory.db').lstat()
        if not stat.S_ISREG(database.st_mode) or (database.st_dev,database.st_ino)!=(int(match.group(3)),int(match.group(4))): return 'unknown_binding'
        lock=index.config_dir.parent/'sios-repair-child.lock'
        fd=os.open(lock,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        try:
            info=os.fstat(fd); named=os.stat(lock,follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or (info.st_dev,info.st_ino)!=(named.st_dev,named.st_ino): return 'unknown_lock'
            if (info.st_dev,info.st_ino)!=(int(match.group(1)),int(match.group(2))): return 'unknown_lock'
            try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: return 'child_active'
            named=os.stat(lock,follow_symlinks=False)
            if (info.st_dev,info.st_ino)!=(named.st_dev,named.st_ino): return 'unknown_lock'
            with store._connection() as db:
                initialize(db)
                n=db.execute('DELETE FROM index_repair_uncertainty WHERE path=? AND token=?',(path,token)).rowcount
            return 'retry_allowed' if n==1 else 'marker_changed'
        finally:
            os.close(fd)
    except (OSError,ValueError,RuntimeError):
        return 'unconfirmed'
