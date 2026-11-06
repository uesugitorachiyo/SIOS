"""Read-only, bounded observation of a candidate repair with unknown outcome."""
import fcntl
import os
import stat
from .index_binding import load_index
from .index_reconcile import source_digest
from .index_retry import initialize


def observe(store, path):
    """Observe one marker; never clear it or claim descendant absence."""
    with store._connection() as db:
        initialize(db)
        marker=db.execute('SELECT revision,token FROM index_repair_uncertainty WHERE path=?',(path,)).fetchone()
    if marker is None:
        return {'status':'no_marker'}
    revision,token=marker
    try:
        index=load_index(store.root)
        lock=index.config_dir.parent/'sios-repair-child.lock'
        # Missing means no evidence, not an unlocked process.
        fd=os.open(lock,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        try:
            info=os.fstat(fd); named=os.stat(lock,follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or (info.st_dev,info.st_ino)!=(named.st_dev,named.st_ino):
                return {'status':'unconfirmed','reason':'lock_identity'}
            try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: return {'status':'unconfirmed','reason':'direct_child_active'}
            before=source_digest(store,path)
            if before!=revision: return {'status':'unconfirmed','reason':'source_changed'}
            index._validate_configuration()
            ready=index._indexed_checksums_match({path.removeprefix('Memory/'):revision})
            after=source_digest(store,path)
            named=os.stat(lock,follow_symlinks=False)
            with store._connection() as db:
                current=db.execute('SELECT revision,token FROM index_repair_uncertainty WHERE path=?',(path,)).fetchone()
            if before!=after or current!=(revision,token) or (info.st_dev,info.st_ino)!=(named.st_dev,named.st_ino):
                return {'status':'unconfirmed','reason':'changed_during_observation'}
            return {'status':'observed','direct_child_lock_free':True,'index_ready':bool(ready),
                    'descendants':'unknown','marker_preserved':True}
        finally:
            os.close(fd)
    except (OSError,ValueError,RuntimeError):
        return {'status':'unconfirmed','reason':'observation_unavailable'}
