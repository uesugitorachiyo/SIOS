import unittest
from unittest.mock import patch
from pathlib import Path
from aios.indexed_note import refresh

class Store:
    root = Path('/fictional-data')
    def read(self, relative): return {'revision':'a'*64,'text':'markerx fresh content'}

class Index:
    def __init__(self, current): self.current=current; self.checks=[]
    async def wait_ready(self, query, timeout, target_path):
        return {'ready':True,'search':{'results':[{'file_path':'notes/fiction.md','matched_chunk':'markerx fresh content','title':'fiction','type':'entity','score':1}]}}
    def _indexed_checksums_match(self, expected): self.checks.append(expected); return self.current

class RefreshTests(unittest.TestCase):
    def test_stale_index_never_reports_ready(self):
        index=Index(False)
        with patch('aios.indexed_note.load_index', return_value=index):
            result=refresh(Store(),'fiction','a'*64,'markerx')
        self.assertFalse(result['ready'])
        self.assertEqual(index.checks,[{'notes/fiction.md':'a'*64}])

    def test_stale_hit_is_not_current_just_because_marker_matches(self):
        index=Index(True)
        async def stale(query, timeout, target_path):
            return {'ready':True,'search':{'results':[{'file_path':'notes/fiction.md','matched_chunk':'markerx obsolete content','title':'fiction','type':'entity','score':1}]}}
        with patch('aios.indexed_note.load_index',return_value=index), patch.object(index,'wait_ready',stale):
            result=refresh(Store(),'fiction','a'*64,'markerx')
        self.assertFalse(result['ready'])

    def test_matching_index_reports_ready(self):
        index=Index(True)
        with patch('aios.indexed_note.load_index', return_value=index):
            result=refresh(Store(),'fiction','a'*64,'markerx')
        self.assertTrue(result['ready'])
        self.assertFalse(result['atomic_snapshot'])
        self.assertEqual(result['index_coverage'], 'unknown')
        self.assertEqual(result['scope'], 'target_marker_index_and_source_observation')
        self.assertEqual(index.checks,[{'notes/fiction.md':'a'*64}])

class RealSqliteTests(unittest.TestCase):
    def test_reused_marker_stale_then_current_index(self):
        import hashlib, sqlite3, tempfile
        from aios.basic_memory import BasicMemoryIndex
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); memory=root/'Memory'; notes=memory/'notes'; notes.mkdir(parents=True)
            index_dir=root/'index'; index_dir.mkdir()
            note=notes/'fiction.md'; note.write_text('markerx new fiction\n')
            rev=hashlib.sha256(note.read_bytes()).hexdigest()
            class LocalStore:
                def __init__(self): self.root=root
                def read(self, relative): return {'revision':rev,'text':note.read_text()}
            idx=BasicMemoryIndex('/bin/true',index_dir,memory,'fixture')
            async def hit(query, timeout, target_path):
                return {'ready':True,'search':{'results':[{'file_path':'notes/fiction.md','matched_chunk':note.read_text().strip(),'title':'fiction','type':'entity','score':1}]}}
            with sqlite3.connect(index_dir/'memory.db') as db:
                db.execute('CREATE TABLE project(id INTEGER,name TEXT,path TEXT)')
                db.execute('CREATE TABLE entity(id INTEGER,project_id INTEGER,file_path TEXT,checksum TEXT)')
                db.execute("CREATE VIRTUAL TABLE search_index USING fts5(id UNINDEXED,project_id UNINDEXED,file_path UNINDEXED,type UNINDEXED,content_snippet,tokenize='unicode61 tokenchars 0x2F')")
                db.execute('INSERT INTO project VALUES(1,?,?)',('fixture',str(memory)))
                db.execute('INSERT INTO entity VALUES(1,1,?,?)',('notes/fiction.md',rev))
                db.execute('INSERT INTO search_index(id,project_id,file_path,type,content_snippet) VALUES(1,1,?,?,?)',('notes/fiction.md','entity','markerx old fiction'))
                db.commit()
                with patch('aios.indexed_note.load_index',return_value=idx), patch.object(idx,'wait_ready',hit):
                    self.assertFalse(refresh(LocalStore(),'fiction',rev,'markerx')['ready'])
                    db.execute('DELETE FROM search_index')
                    db.execute('INSERT INTO search_index(id,project_id,file_path,type,content_snippet) VALUES(1,1,?,?,?)',('notes/fiction.md','entity','markerx new fiction'))
                    db.commit()
                    self.assertTrue(refresh(LocalStore(),'fiction',rev,'markerx')['ready'])
