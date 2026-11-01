"""Indexed Memory hits provide an exact safe path for Store.read."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aios.basic_memory import BasicMemoryError
from aios.service import dispatch
from aios.store import Store


class IndexSearchReadPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sios-index-read-path-')
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name).resolve() / 'data')
        self.note = self.store.root / 'Memory' / 'sessions' / 'caf\u00e9 progress \u2014 2026-10-20.md'
        self.note.parent.mkdir(parents=True)
        self.note.write_text('# Fixture progress\n')

    def search(self, result):
        class Index:
            def search(self, query):
                return result
        with patch('aios.index_binding.load_index', return_value=Index()):
            return dispatch(self.store, 'memory_index_search', {'query': 'progress'},
                            actor='reader', scopes={'read'})

    def test_relative_hit_retains_raw_fields_and_adds_readable_path(self):
        raw = self.note.relative_to(self.store.root / 'Memory').as_posix()
        response = {'results': [{'file_path': raw, 'title': 'Fixture', 'score': 0.9}],
                    'total': 1}
        result = self.search(response)
        hit = result['results'][0]
        self.assertEqual(hit['file_path'], raw)
        self.assertEqual(hit['title'], 'Fixture')
        self.assertEqual(hit['score'], 0.9)
        self.assertEqual(hit['read_path'], 'Memory/' + raw)
        self.assertEqual(self.store.read(hit['read_path'])['text'], '# Fixture progress\n')
        self.assertNotIn('read_path', response['results'][0])

    def test_prefixed_absolute_and_enveloped_shapes(self):
        relative = self.note.relative_to(self.store.root).as_posix()
        for raw in (relative, str(self.note)):
            with self.subTest(raw=raw):
                result = self.search({'result': {'results': [{'file_path': raw}], 'total': 1}})
                self.assertEqual(result['result']['results'][0]['read_path'], relative)
                self.assertEqual(result['result']['results'][0]['file_path'], raw)

    def test_outside_traversal_and_linked_paths_fail_closed(self):
        outside = self.store.root / 'Missions' / 'unrelated.md'
        for raw in ('../Missions/unrelated.md', 'Memory/../Missions/unrelated.md',
                    str(outside), 'sessions/./note.md', 'sessions\\note.md'):
            with self.subTest(raw=raw), self.assertRaises(BasicMemoryError):
                self.search({'results': [{'file_path': raw}]})
        link = self.note.parent / 'linked.md'
        link.symlink_to(self.note)
        with self.assertRaises(BasicMemoryError):
            self.search({'results': [{'file_path': 'sessions/linked.md'}]})

    def test_conflicting_read_path_fails_closed(self):
        with self.assertRaises(BasicMemoryError):
            self.search({'results': [{'file_path': 'sessions/note.md',
                                      'read_path': 'Memory/other.md'}]})


if __name__ == '__main__':
    unittest.main()
