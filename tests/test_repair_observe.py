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
from aios.index_repair_observe import observe
from aios.index_retry import initialize
from aios.store import Store

class ObserveRepair(unittest.TestCase):
    def test_active_child_and_changed_source_preserve_marker(self):
        with tempfile.TemporaryDirectory(prefix='sios-observe-') as name:
            root=Path(name).resolve(); store=Store(root/'data'); index_dir=root/'index';index_dir.mkdir()
            lock=root/'sios-repair-child.lock'; fd=os.open(lock,os.O_CREAT|os.O_RDWR,0o600)
            self.addCleanup(os.close,fd)
            path='Memory/notes/fiction.md'; rev='a'*64
            with store._connection() as db:
                initialize(db)
                db.execute('INSERT INTO index_repair_uncertainty VALUES(?,?,?)',(path,rev,'token'))
            class Index:
                config_dir=index_dir
                def _validate_configuration(self): pass
                def _indexed_checksums_match(self, expected): return True
            with patch('aios.index_repair_observe.load_index',return_value=Index()), \
                 patch('aios.index_repair_observe.source_digest',return_value='b'*64):
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.assertEqual(observe(store,path)['reason'],'direct_child_active')
                fcntl.flock(fd,fcntl.LOCK_UN)
                self.assertEqual(observe(store,path)['reason'],'source_changed')
            with store._connection() as db:
                self.assertEqual(db.execute('SELECT token FROM index_repair_uncertainty').fetchone(),('token',))
