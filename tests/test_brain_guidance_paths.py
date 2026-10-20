"""Only the three reviewed Brain operating documents can enter Guidance."""

import json
import tempfile
import unittest
from pathlib import Path

from aios.service import dispatch
from aios.store import Store, ValidationError


class BrainGuidancePathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sios-brain-guidance-')
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name).resolve() / 'data')
        (self.store.root / 'Guidance').mkdir()
        self.paths = (
            'Brain/AGENTS.md',
            'Brain/workflows/review-candidates.md',
            'Brain/workflows/promote-memory.md',
        )
        for relative in self.paths:
            target = self.store.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('# Synthetic operating guidance\n')

    def register(self, paths):
        (self.store.root / 'Guidance' / 'index.json').write_text(json.dumps({
            'version': 1,
            'documents': [{'path': path, 'when': 'Relevant Brain review'} for path in paths],
        }))

    def test_exact_paths_require_registry_and_read_scope(self):
        for path in self.paths:
            with self.assertRaises(ValidationError):
                self.store.read(path)
        self.register(self.paths)
        for path in self.paths:
            self.assertEqual(dispatch(self.store, 'read', {'relative': path},
                                      actor='reader', scopes={'read'})['path'], path)
        with self.assertRaises(PermissionError):
            dispatch(self.store, 'read', {'relative': self.paths[0]},
                     actor='writer', scopes={'missions:write'})

    def test_neighbor_and_traversal_are_denied(self):
        self.register(self.paths)
        neighbor = 'Brain/workflows/other.md'
        (self.store.root / neighbor).write_text('# not registered\n')
        with self.assertRaises(ValidationError):
            self.store.read(neighbor)
        with self.assertRaises(ValidationError):
            self.store.read('Brain/workflows/../AGENTS.md')
        self.register((*self.paths, neighbor))
        with self.assertRaisesRegex(ValidationError, 'outside bounded guidance roots'):
            self.store.read(self.paths[0])


if __name__ == '__main__':
    unittest.main()
