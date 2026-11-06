import tempfile
import subprocess
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
from aios.index_retry import initialize, retry_one, begin, end, enqueue_intents, _due, complete
from aios.store import Store

class UncertainRepair(unittest.TestCase):
    def test_process_death_keeps_marker_and_other_job_available(self):
        with tempfile.TemporaryDirectory(prefix='sios-marker-crash-') as name:
            root=Path(name).resolve()/'data'; store=Store(root)
            path='Memory/notes/a.md'; rev='a'*64
            with store._connection() as db:
                initialize(db)
                db.execute('INSERT INTO index_retry_jobs VALUES(?,?,?,?,?)',(path,rev,0,0,None))
                db.execute('INSERT INTO index_retry_jobs VALUES(?,?,?,?,?)',('Memory/notes/b.md','b'*64,0,1,None))
            program="""import os,sys\nfrom aios.store import Store\nfrom aios.index_retry import _mark_repair_unknown\ns=Store(sys.argv[1]); assert _mark_repair_unknown(s,'Memory/notes/a.md','a'*64); os._exit(19)\n"""
            result=subprocess.run([sys.executable,'-c',program,str(root)],capture_output=True,timeout=5)
            self.assertEqual(result.returncode,19,result.stderr.decode())
            restarted=Store(root)
            with restarted._connection() as db:
                self.assertEqual(db.execute('SELECT path,revision FROM index_repair_uncertainty').fetchone(),(path,rev))
            self.assertEqual(_due(restarted,10**12)[0],'Memory/notes/b.md')

    def test_unknown_is_durable_and_no_second_repair_is_started(self):
        with tempfile.TemporaryDirectory(prefix='sios-unknown-') as name:
            store=Store(Path(name).resolve()/'data')
            path='Memory/notes/fiction.md'; rev='a'*64
            with store._connection() as db:
                initialize(db)
                db.execute('INSERT INTO index_retry_jobs VALUES(?,?,?,?,?)',(path,rev,0,0,None))
            class Index:
                async def wait_for_files(self, expected, timeout): return False
            calls=[]
            def failed(*args, **kwargs):
                calls.append(1)
                raise TimeoutError('outcome unknown')
            with patch('aios.index_reconcile.reconcile',return_value=0), \
                 patch('aios.index_reconcile.source_digest',return_value=rev), \
                 patch('aios.index_binding.load_index',return_value=Index()):
                self.assertTrue(retry_one(store,now=10**12,repair=failed))
                with store._connection() as db:
                    self.assertEqual(db.execute('SELECT revision FROM index_repair_uncertainty').fetchone()[0], rev)
                self.assertFalse(retry_one(store,now=10**12,repair=failed))
                with patch('aios.index_reconcile.source_digest',return_value='b'*64):
                    self.assertFalse(retry_one(store,now=10**12,repair=failed))
                token=begin({'read','memory:write'})
                try:
                    with store._connection() as db:
                        enqueue_intents(db,[{'path':path,'text':'new fictional revision'}])
                finally:
                    end(token)
                with store._connection() as db:
                    new_rev=db.execute('SELECT revision FROM index_retry_jobs').fetchone()[0]
                    self.assertNotEqual(new_rev,rev)
                with patch('aios.index_reconcile.source_digest',return_value=new_rev):
                    self.assertFalse(retry_one(store,now=10**12,repair=failed))
                with store._connection() as db:
                    db.execute('INSERT INTO index_retry_jobs VALUES(?,?,?,?,?)',('Memory/notes/other.md','c'*64,0,1,None))
                self.assertEqual(_due(store,10**12)[0],'Memory/notes/other.md')
            self.assertEqual(calls,[1])
            with store._connection() as db:
                self.assertEqual(db.execute('SELECT revision FROM index_repair_uncertainty').fetchone(), (rev,))

    def test_concurrent_completion_retains_job_until_uncertainty_resolved(self):
        with tempfile.TemporaryDirectory(prefix='sios-completion-race-') as name:
            store=Store(Path(name).resolve()/'data')
            path='Memory/notes/fiction.md'; old='a'*64; new='b'*64
            with store._connection() as db:
                initialize(db)
                db.execute('INSERT INTO index_retry_jobs VALUES(?,?,?,?,?)',(path,new,0,0,None))
                db.execute('INSERT INTO index_repair_uncertainty VALUES(?,?,?)',(path,old,'unknown1-token'))
            complete(store,path,new)
            with store._connection() as db:
                self.assertEqual(db.execute('SELECT revision FROM index_retry_jobs WHERE path=?',(path,)).fetchone(),(new,))
                db.execute('DELETE FROM index_repair_uncertainty WHERE path=?',(path,))
            complete(store,path,old)
            with store._connection() as db:
                self.assertEqual(db.execute('SELECT revision FROM index_retry_jobs WHERE path=?',(path,)).fetchone(),(new,))
            complete(store,path,new)
            with store._connection() as db:
                self.assertIsNone(db.execute('SELECT revision FROM index_retry_jobs WHERE path=?',(path,)).fetchone())
