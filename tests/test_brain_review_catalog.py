import tempfile
import unittest
from pathlib import Path

from aios.store import Store, ValidationError
from aios.service import dispatch


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'data'
        for relative in ('Brain/working/candidates', 'Brain/inbox', 'Missions'):
            (self.root / relative).mkdir(parents=True)
        self.store = Store(self.root)

    def test_pending_and_inbox_only(self):
        (self.root / 'Brain/working/candidates/pending.md').write_text('pending')
        (self.root / 'Brain/working/candidates/README.md').write_text('reference')
        (self.root / 'Brain/working/candidates/REVIEW-old.md').write_text('history')
        (self.root / 'Brain/working/candidates/promoted-2026').mkdir()
        (self.root / 'Brain/working/candidates/promoted-2026/old.md').write_text('history')
        (self.root / 'Brain/inbox/new.md').write_text('new')
        (self.root / 'Brain/inbox/README.md').write_text('reference')
        result = self.store.catalog('brain-review')
        self.assertEqual(result['paths'], ['Brain/working/candidates/pending.md', 'Brain/inbox/new.md'])
        self.assertEqual(result['authority_status'], 'pending_or_source_not_canonical')

    def test_symlink_rejected(self):
        (self.root / 'Brain/inbox/link.md').symlink_to(self.root / 'Brain/inbox/target.md')
        with self.assertRaises(ValidationError):
            self.store.catalog('brain-review')

    def test_oversized_item_rejected(self):
        with (self.root / 'Brain/inbox/large.md').open('wb') as stream:
            stream.truncate(2_000_001)
        with self.assertRaises(ValidationError):
            self.store.catalog('brain-review')

    def test_unrecognized_store_rejected(self):
        with self.assertRaises(ValidationError):
            self.store.catalog('brain')

    def test_oversized_mission_log_revision_without_body(self):
        mission = self.root / 'Missions/example'
        mission.mkdir()
        data = b'x' * 2_000_001
        (mission / 'log.md').write_bytes(data)
        result = self.store.read_revision('Missions/example/log.md')
        import hashlib
        self.assertEqual(result['revision'], hashlib.sha256(data).hexdigest())
        self.assertEqual(result['bytes'], len(data))
        self.assertFalse(result['content_returned'])
        with self.assertRaises(ValidationError):
            self.store.read('Missions/example/log.md')

    def test_revision_denies_other_roots(self):
        with self.assertRaises(ValidationError):
            self.store.read_revision('Brain/working/candidates/pending.md')

    def test_existing_read_scope_dispatches_both_operations(self):
        (self.root / 'Brain/inbox/new.md').write_text('review')
        (self.root / 'Missions/example').mkdir()
        (self.root / 'Missions/example/log.md').write_text('log')
        listing = dispatch(self.store, 'catalog', {'store': 'brain-review'}, actor='fixture', scopes={'read'})
        revision = dispatch(self.store, 'read_revision', {'relative': 'Missions/example/log.md'},
                            actor='fixture', scopes={'read'})
        self.assertEqual(listing['paths'], ['Brain/inbox/new.md'])
        self.assertEqual(revision['bytes'], 3)


if __name__ == '__main__':
    unittest.main()
