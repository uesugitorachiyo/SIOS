import sys
import unittest
if sys.platform != 'darwin':
    raise unittest.SkipTest('macOS-specific repair and file locking')
import fcntl
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from aios.index_repair_resume import reconcile_one
from aios.index_retry import initialize
from aios.store import Store

class ResumeRepair(unittest.TestCase):
    def test_live_child_blocks_resume_then_marker_released_but_job_remains(self):
        with tempfile.TemporaryDirectory(prefix='sios-resume-') as name:
            root=Path(name).resolve(); store=Store(root/'data'); index=root/'index'; index.mkdir()
            fd=os.open(root/'sios-repair-child.lock',os.O_CREAT|os.O_RDWR,0o600)
            self.addCleanup(os.close,fd)
            info=os.fstat(fd); (index/'memory.db').write_text('fixture'); dbinfo=(index/'memory.db').stat()
            path='Memory/notes/a.md'; rev='a'*64; token=f'nofork2-{info.st_dev}-{info.st_ino}-{dbinfo.st_dev}-{dbinfo.st_ino}-'+ '1'*32
            with store._connection() as db:
                initialize(db)
                db.execute('INSERT INTO index_retry_jobs VALUES(?,?,?,?,?)',(path,rev,0,0,None))
                db.execute('INSERT INTO index_repair_uncertainty VALUES(?,?,?)',(path,rev,token))
            class Index:
                config_dir=index
                def _validate_configuration(self): pass
            with patch('aios.index_repair_resume.load_index',return_value=Index()):
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.assertEqual(reconcile_one(store,path),'child_active')
                fcntl.flock(fd,fcntl.LOCK_UN)
                self.assertEqual(reconcile_one(store,path),'retry_allowed')
            with store._connection() as db:
                self.assertIsNone(db.execute('SELECT token FROM index_repair_uncertainty').fetchone())
                self.assertEqual(db.execute('SELECT path,revision FROM index_retry_jobs').fetchone(),(path,rev))

    def test_other_protocol_is_not_cleared(self):
        with tempfile.TemporaryDirectory(prefix='sios-resume-') as name:
            store=Store(Path(name).resolve()/'data'); path='Memory/notes/a.md'
            with store._connection() as db:
                initialize(db); db.execute('INSERT INTO index_repair_uncertainty VALUES(?,?,?)',(path,'a'*64,'older-token'))
            self.assertEqual(reconcile_one(store,path),'unknown_marker')
            with store._connection() as db:
                self.assertEqual(db.execute('SELECT token FROM index_repair_uncertainty').fetchone(),('older-token',))

    def test_replaced_lock_never_releases_marker(self):
        with tempfile.TemporaryDirectory(prefix='sios-resume-') as name:
            root=Path(name).resolve(); store=Store(root/'data'); index=root/'index'; index.mkdir()
            lock=root/'sios-repair-child.lock'; lock.write_text('old'); info=lock.stat()
            (index/'memory.db').write_text('fixture'); dbinfo=(index/'memory.db').stat()
            path='Memory/notes/a.md'; token=f'nofork2-{info.st_dev}-{info.st_ino}-{dbinfo.st_dev}-{dbinfo.st_ino}-'+'1'*32
            with store._connection() as db:
                initialize(db); db.execute('INSERT INTO index_repair_uncertainty VALUES(?,?,?)',(path,'a'*64,token))
            old=root/'old.lock'; lock.rename(old); lock.write_text('new')
            class Index:
                config_dir=index
                def _validate_configuration(self): pass
            with patch('aios.index_repair_resume.load_index',return_value=Index()):
                self.assertEqual(reconcile_one(store,path),'unknown_lock')
            with store._connection() as db:
                self.assertEqual(db.execute('SELECT token FROM index_repair_uncertainty').fetchone(),(token,))
