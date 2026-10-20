"""Mission enumeration exposes only bounded body paths under existing read scope."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aios.service import dispatch
from aios.store import Store, ValidationError


class MissionCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sios-catalog-')
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name).resolve() / 'data')
        missions = self.store.root / 'Missions'
        missions.mkdir()
        for name in ('alpha', 'beta'):
            folder = missions / name
            folder.mkdir()
            (folder / 'mission.md').write_text(f'# {name}\n')
            (folder / 'log.md').write_text('# historical log\n')
        (missions / 'beta' / 'evidence').mkdir()
        (missions / 'beta' / 'evidence' / 'note.md').write_text('# attachment\n')
        (missions / 'loose.md').write_text('# no Mission\n')

    def test_only_mission_bodies_and_read_scope(self):
        expected = {'store': 'missions', 'paths': [
            'Missions/alpha/mission.md', 'Missions/beta/mission.md']}
        self.assertEqual(self.store.catalog('missions'), expected)
        self.assertEqual(dispatch(self.store, 'catalog', {'store': 'missions'},
                                  actor='reader', scopes={'read'}), expected)
        with self.assertRaises(PermissionError):
            dispatch(self.store, 'catalog', {'store': 'missions'},
                     actor='writer', scopes={'missions:write'})
        with self.assertRaises(ValidationError):
            self.store.catalog('brain_candidates')
        with self.assertRaises(ValidationError):
            dispatch(self.store, 'catalog', {'store': 'missions', 'actor': 'owner'},
                     actor='reader', scopes={'read'})

    def test_rejects_links_including_hidden_and_incomplete_walk(self):
        linked = self.store.root / 'Missions' / '.hidden-link'
        linked.symlink_to(self.store.root / 'Missions' / 'alpha', target_is_directory=True)
        with self.assertRaises(ValidationError):
            self.store.catalog('missions')
        linked.unlink()
        (self.store.root / 'Missions' / 'alpha' / 'mission.md').unlink()
        (self.store.root / 'Missions' / 'alpha' / 'mission.md').symlink_to(
            self.store.root / 'Missions' / 'beta' / 'mission.md')
        with self.assertRaises(ValidationError):
            self.store.catalog('missions')
        (self.store.root / 'Missions' / 'alpha' / 'mission.md').unlink()
        with patch.object(Path, 'iterdir', side_effect=OSError('fixture unreadable')):
            with self.assertRaisesRegex(ValidationError, 'Incomplete Mission catalog'):
                self.store.catalog('missions')

    def test_entry_limit_is_fail_closed(self):
        with patch('aios.store.MAX_MISSION_CATALOG_ITEMS', 1):
            with self.assertRaisesRegex(ValidationError, 'response limit'):
                self.store.catalog('missions')


if __name__ == '__main__':
    unittest.main()
