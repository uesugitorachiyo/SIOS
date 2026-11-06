import sys
import unittest
if sys.platform != 'darwin':
    raise unittest.SkipTest('macOS-specific repair and file locking')
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from aios.index_repair_100 import repair
from aios.index_retry import initialize, _mark_repair_unknown
from aios.store import Store

class IdentityControls(unittest.TestCase):
    def test_marker_promoted_while_lock_held_before_child_result(self):
        with tempfile.TemporaryDirectory(prefix='sios-promote-') as name:
            root=Path(name).resolve(); store=Store(root/'data'); memory=store.root/'Memory'; memory.mkdir(exist_ok=True)
            index_dir=root/'index'; index_dir.mkdir(); (index_dir/'config.json').write_text('config')
            (store.root/'.aios-basic-memory.json').write_text('original')
            source=memory/'note.md'; source.write_text('fictional'); rev=hashlib.sha256(b'fictional').hexdigest()
            with sqlite3.connect(index_dir/'memory.db') as db:
                db.execute('CREATE TABLE project(external_id TEXT,name TEXT,path TEXT)')
                db.execute('INSERT INTO project VALUES(?,?,?)',('fake-id','fiction',str(memory)))
            path='Memory/note.md'
            with store._connection() as db:
                initialize(db);db.execute('INSERT INTO index_retry_jobs VALUES(?,?,?,?,?)',(path,rev,0,0,None))
            token=_mark_repair_unknown(store,path,rev)
            class Index:
                memory_root=memory; config_dir=index_dir; project='fiction'
                def _validate_configuration(self): pass
                def _environment(self): return {}
            class Child:
                def wait(self, timeout): raise SystemExit('synthetic interruption')
            with patch('aios.index_repair_100.subprocess.Popen',return_value=Child()):
                with self.assertRaises(SystemExit):
                    repair(Index(),rel_path='note.md',expected_revision=rev,python='fixture',vendor='fixture',cache='fixture',
                           store=store,marker_token=token)
            with store._connection() as db:
                promoted=db.execute('SELECT token FROM index_repair_uncertainty WHERE path=?',(path,)).fetchone()[0]
            self.assertTrue(promoted.startswith('nofork2-'))

    def test_changed_binding_or_replaced_lock_is_unconfirmed(self):
        for change in ('binding','lock','source'):
            with self.subTest(change=change), tempfile.TemporaryDirectory(prefix='sios-bind-') as name:
                root=Path(name).resolve(); memory=root/'Memory'; memory.mkdir(); index_dir=root/'index'; index_dir.mkdir()
                (root/'.aios-basic-memory.json').write_text('original')
                (index_dir/'config.json').write_text('config')
                source=memory/'note.md'; source.write_text('fictional')
                with sqlite3.connect(index_dir/'memory.db') as db:
                    db.execute('CREATE TABLE project(external_id TEXT,name TEXT,path TEXT)')
                    db.execute('INSERT INTO project VALUES(?,?,?)',('fake-id','fiction',str(memory)))
                class Index:
                    memory_root=memory; config_dir=index_dir; project='fiction'
                    def _validate_configuration(self): pass
                    def _environment(self): return {}
                class Child:
                    def wait(self, timeout):
                        if change=='binding': (root/'.aios-basic-memory.json').write_text('changed')
                        if change=='source': source.write_text('changed')
                        if change=='lock':
                            (root/'sios-repair-child.lock').unlink()
                            (root/'sios-repair-child.lock').write_text('replaced')
                        return 0
                with patch('aios.index_repair_100.subprocess.Popen',return_value=Child()):
                    with self.assertRaises(ValueError):
                        repair(Index(),rel_path='note.md',expected_revision=hashlib.sha256(b'fictional').hexdigest(),
                               python='fixture',vendor='fixture',cache='fixture')
