"""Independent tests against synthetic WAL stores and disposable keys only."""
import base64
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import psutil
import aios.backup as candidate
from aios.store import Store,ValidationError



class BackupReview(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='sios-wal-review-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.store=Store(self.root/'data')
        (self.store.root/'.aios-instance.json').write_text(json.dumps({'schema_version':1,'instance_id':str(uuid.uuid4())}))
        self.path=self.store.root/'Database/state'
        self.writer=sqlite3.connect(self.path)
        self.addCleanup(self.writer.close)
        self.assertEqual(self.writer.execute('PRAGMA journal_mode=WAL').fetchone()[0],'wal')
        self.writer.execute('PRAGMA wal_autocheckpoint=0')
        self.writer.execute('CREATE TABLE fixture_data(value BLOB)'); self.writer.commit()
        self.writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()

    def test_actual_backup_blocks_competing_writer_and_keeps_snapshot(self):
        payload=b'fictional'*350000
        self.writer.execute('INSERT INTO fixture_data VALUES(?)',(payload,));self.writer.commit()
        logical=self.writer.execute('PRAGMA page_count').fetchone()[0]*self.writer.execute('PRAGMA page_size').fetchone()[0]
        self.assertGreater(logical,self.path.stat().st_size)
        observed=[];original=candidate._admit_large_archive
        def admission(size):
            if size>=logical and not observed:
                def compete():
                    with closing(sqlite3.connect(self.path,timeout=.2)) as db:
                        try:
                            db.execute("INSERT INTO fixture_data VALUES('late')");db.commit()
                            observed.append('unexpected commit')
                        except sqlite3.OperationalError as exc:
                            observed.append(str(exc));db.rollback()
                thread=threading.Thread(target=compete);thread.start();thread.join(3)
                self.assertFalse(thread.is_alive())
            return original(size)
        key=os.urandom(32); archive=self.root/'test.enc'
        with patch.object(candidate,'_admit_large_archive',side_effect=admission):
            candidate.backup(self.store,archive,key)
        self.assertEqual(observed,['database is locked'])
        self.writer.execute("INSERT INTO fixture_data VALUES('late')");self.writer.commit()
        candidate.restore(archive,self.root/'restored',key)
        with closing(sqlite3.connect(self.root/'restored/Database/state')) as db:
            self.assertEqual(db.execute('SELECT value FROM fixture_data').fetchall(),[(payload,)])
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone(),('ok',))
        self.assertEqual(self.writer.execute('SELECT count(*) FROM fixture_data').fetchone(),(2,))

    def test_admission_rejects_logical_wal_size_before_memory_snapshot(self):
        for _ in range(65): self.writer.execute('INSERT INTO fixture_data VALUES(zeroblob(1048576))')
        self.writer.commit()
        logical=self.writer.execute('PRAGMA page_count').fetchone()[0]*self.writer.execute('PRAGMA page_size').fetchone()[0]
        self.assertLess(self.path.stat().st_size,64*1024*1024)
        self.assertGreater(logical,64*1024*1024)
        real=sqlite3.connect;memory_connections=[]
        def connect(*args,**kwargs):
            if args and args[0]==':memory:': memory_connections.append(True)
            return real(*args,**kwargs)
        with patch.object(psutil,'virtual_memory',return_value=SimpleNamespace(available=1)), \
             patch.object(candidate.sqlite3,'connect',side_effect=connect):
            with self.assertRaisesRegex(ValidationError,'Insufficient available memory'):
                candidate.backup(self.store,self.root/'rejected.enc',os.urandom(32))
        self.assertFalse(memory_connections)
        self.assertFalse((self.root/'rejected.enc').exists())

    def test_legacy_wal_archives_preserve_authenticated_bytes(self):
        self.writer.execute("INSERT INTO fixture_data VALUES('fictional')"); self.writer.commit()
        key = os.urandom(32); archive = self.root/'new.enc'
        candidate.backup(self.store, archive, key)
        encrypted = archive.read_bytes()
        document = json.loads(candidate._crypto(key).decrypt(
            encrypted[len(candidate.MAGIC):len(candidate.MAGIC)+12],
            encrypted[len(candidate.MAGIC)+12:], candidate.MAGIC))
        image = base64.b64decode(document['files'][candidate.DATABASE]['content'])
        wal_image = image[:18] + b'\x02\x02' + image[20:]
        document['files'][candidate.DATABASE]['content'] = base64.b64encode(wal_image).decode()
        import hashlib
        document['files'][candidate.DATABASE]['sha256'] = hashlib.sha256(wal_image).hexdigest()
        for schema in (1, 2):
            record = json.loads(json.dumps(document)); record['schema_version'] = schema
            if schema == 1:
                record.pop('directories')
                for item in record['files'].values(): item.pop('executable')
            nonce = os.urandom(12)
            legacy = self.root/f'legacy-{schema}.enc'
            legacy.write_bytes(candidate.MAGIC + nonce + candidate._crypto(key).encrypt(
                nonce, candidate._json(record), candidate.MAGIC))
            target = self.root/f'restored-{schema}'
            candidate.restore(legacy, target, key)
            restored = target/'Database/state'
            self.assertEqual(restored.read_bytes(), wal_image)
            with closing(sqlite3.connect(restored)) as db:
                self.assertEqual(db.execute('SELECT value FROM fixture_data').fetchall(), [('fictional',)])

    def test_header_normalization_only_validates_known_modes(self):
        image=b'SQLite format 3\x00'+b'\x10\x00'+b'\x02\x02'+bytes(80)
        normalized=candidate._standalone_sqlite(image)
        self.assertEqual(normalized[:18],image[:18]);self.assertEqual(normalized[18:20],b'\x01\x01')
        self.assertEqual(normalized[20:],image[20:]);self.assertEqual(image[18:20],b'\x02\x02')
        with self.assertRaises(ValidationError): candidate._standalone_sqlite(image[:18]+b'\x02\x01'+image[20:])
        with self.assertRaises(ValidationError): candidate._standalone_sqlite(b'not sqlite')

    def test_invalid_positive_page_sizes_fail_before_in_memory_snapshot(self):
        real=sqlite3.connect
        for page_size in (1,1000,131072):
            with self.subTest(page_size=page_size):
                memory=[]
                class Cursor:
                    def fetchone(self): return (page_size,)
                class Reader:
                    def __init__(self,db): self.db=db
                    def execute(self,sql,*args):
                        if sql=='PRAGMA page_size': return Cursor()
                        return self.db.execute(sql,*args)
                    def close(self): self.db.close()
                def connect(*args,**kwargs):
                    if args and args[0]==':memory:': memory.append(True)
                    db=real(*args,**kwargs)
                    if args and Path(args[0])==self.path and not kwargs: return Reader(db)
                    return db
                with patch.object(candidate.sqlite3,'connect',side_effect=connect):
                    with self.assertRaisesRegex(ValidationError,'Invalid SQLite snapshot size'):
                        candidate.backup(self.store,self.root/f'rejected-{page_size}.enc',os.urandom(32))
                self.assertFalse(memory)
                self.assertFalse((self.root/f'rejected-{page_size}.enc').exists())

    def test_fault_injected_page_count_undercount_is_caught_after_allocation(self):
        self.writer.execute('INSERT INTO fixture_data VALUES(zeroblob(1048576))');self.writer.commit()
        real=sqlite3.connect; memory=[]
        class Cursor:
            def fetchone(self): return (1,)
        class Reader:
            def __init__(self,db): self.db=db
            def execute(self,sql,*args):
                if sql=='PRAGMA page_count': return Cursor()
                return self.db.execute(sql,*args)
            def backup(self,target): return self.db.backup(target)
            def close(self): self.db.close()
        def connect(*args,**kwargs):
            if args and args[0]==':memory:': memory.append(True)
            db=real(*args,**kwargs)
            if args and Path(args[0])==self.path and not kwargs: return Reader(db)
            return db
        with patch.object(candidate.sqlite3,'connect',side_effect=connect):
            with self.assertRaisesRegex(ValidationError,'SQLite snapshot size'):
                candidate.backup(self.store,self.root/'undercount.enc',os.urandom(32))
        self.assertTrue(memory,'fault injection must reach a snapshot allocation')
        self.assertFalse((self.root/'undercount.enc').exists())
