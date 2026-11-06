"""Fictional tests for the explicitly selected Basic Memory 1.0 adapter."""
import asyncio
import hashlib
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aios.basic_memory import BasicMemoryError
from aios.basic_memory_100 import BasicMemoryIndex
from aios.basic_memory_100_launcher import _check_upstream, _memory_root
from aios.config import Config
from aios.index_binding import load_index, setup_index
from aios.service import dispatch
from aios.store import Store


class LauncherGuards(unittest.TestCase):
    def test_unexpected_upstream_version_is_rejected(self):
        with patch('aios.basic_memory_100_launcher.metadata.version', return_value='1.0.1'):
            with self.assertRaises(RuntimeError):
                _check_upstream()

    def test_missing_marker_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'BASIC_MEMORY_CONFIG_DIR': temp}):
            with self.assertRaises(OSError):
                _memory_root()


@unittest.skipUnless(os.environ.get('SIOS_TEST_BM100_LAUNCHER'), 'requires disposable native 1.0 launcher')
class NativeIndex(unittest.TestCase):
    def test_index_and_reindex_preserve_originals(self):
        with tempfile.TemporaryDirectory(prefix='sios-bm100-') as tmp:
            root = Path(tmp).resolve()
            notes = root / 'notes'; notes.mkdir()
            docs = {'a.md': '# Alpha\n\nfictionalmarker initial\n',
                    'b.md': '---\ntype: note\n---\n# Beta\n\nfictionalmarker second\n'}
            for name, body in docs.items():
                (notes / name).write_text(body, encoding='utf-8')
            digest = lambda body: hashlib.sha256(body.encode()).hexdigest()
            expected = {name: digest(body) for name, body in docs.items()}
            index = BasicMemoryIndex(os.environ['SIOS_TEST_BM100_LAUNCHER'], root / 'index', notes, 'fiction')
            self.assertTrue(index.setup()['ok'])
            self.assertTrue(asyncio.run(index.wait_for_files(expected, timeout=7)))
            self.assertEqual(expected, {name: hashlib.sha256((notes/name).read_bytes()).hexdigest() for name in docs})
            docs['a.md'] = '# Alpha\n\nfictionalmarker updated\n'
            (notes / 'a.md').write_text(docs['a.md'], encoding='utf-8')
            expected['a.md'] = digest(docs['a.md'])
            self.assertTrue(asyncio.run(index.wait_for_files(expected, timeout=7)))
            self.assertEqual(expected, {name: hashlib.sha256((notes/name).read_bytes()).hexdigest() for name in docs})

    def test_binding_capture_and_lookup(self):
        with tempfile.TemporaryDirectory(prefix='sios-bm100-binding-') as tmp:
            root = Path(tmp).resolve()
            store = Store(root / 'data'); (store.root / 'Memory').mkdir(exist_ok=True)
            cfg = Config('standalone', '00000000-0000-4000-8000-000000000001',
                         store.root, root/'cache', 'UTC', None, root/'config.json')
            setup = setup_index(cfg, os.environ['SIOS_TEST_BM100_LAUNCHER'], root/'index', version='1.0.0')
            self.assertTrue(setup['ok'])
            self.assertIsInstance(load_index(store.root), BasicMemoryIndex)
            request = {'retain':True,'kind':'delta','meaning_id':'fiction-index',
                       'claim':'fictionaluniquesearchmarker original', 'source_type':'observed',
                       'source_actor':'fixture','source_reference':'https://example.com/fixture',
                       'project':'fiction','conditions':['fiction only'],'outcome':'fixture',
                       'evidence_status':'observed','recorded_at':'2026-11-06T00:00:00Z',
                       'retrieval_cue':'fictionaluniquesearchmarker','request_id':'fixture-index-100'}
            result = dispatch(store, 'memory_capture', {'request_json':json.dumps(request)},
                              actor='fixture', scopes={'read','memory:write'})
            self.assertEqual(result['status'], 'captured')
            self.assertFalse(result['index_refresh']['ready'])
            found = dispatch(store, 'memory_index_search', {'query':Path(result['path']).stem},
                             actor='fixture', scopes={'read'})
            self.assertTrue(any(hit.get('read_path') == result['path'] for hit in found['results']))
            natural = dispatch(store, 'memory_index_search', {'query':'fictionaluniquesearchmarker'},
                               actor='fixture', scopes={'read'})
            self.assertTrue(any(hit.get('read_path') == result['path'] for hit in natural['results']))
            self.assertEqual(store.read(result['path'])['revision'], result['revision'])
            from aios.bounded_body_proof import BodyProof
            def rpc(operation, params):
                return dispatch(store, operation, params, actor='fixture', scopes={'read'})
            proof = BodyProof(rpc, timeout=15).check(result['path'], result['revision'],
                                                    'fictionaluniquesearchmarker')
            self.assertTrue(proof['ready'], proof)
            # Remove only this fictional FTS row; repair must restore it while
            # leaving the committed source bytes unchanged.
            index = load_index(store.root)
            relative = result['path'].removeprefix('Memory/')
            with sqlite3.connect(index.config_dir / 'memory.db') as db:
                db.execute('DELETE FROM search_index WHERE file_path=?', (relative,))
            self.assertFalse(index._indexed_checksums_match({relative: result['revision']}))
            source = store.root / result['path']
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            from aios.index_repair_100 import repair
            venv = Path(os.environ['SIOS_TEST_BM100_LAUNCHER']).parent
            cache = root / 'repair-cache'; cache.mkdir()
            vendor = root / 'trusted-imports'; vendor.mkdir()
            repaired = repair(index, rel_path=relative, expected_revision=result['revision'],
                              python=venv / 'python', vendor=vendor, cache=cache, timeout=40)
            self.assertTrue(repaired['settled'])
            self.assertTrue(index._indexed_checksums_match({relative: result['revision']}))
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
