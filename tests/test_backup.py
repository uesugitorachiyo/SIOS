"""Synthetic recovery checks for the bounded local snapshot."""

import os
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
import uuid

from aios.backup import backup, restore
from aios.service import dispatch
from aios.store import Store, ValidationError


class BackupTests(unittest.TestCase):
    GRANTS = {'mac': {'threads:create', 'threads:post', 'threads:read', 'threads:ack', 'threads:manage'}}
    PROJECTS = {'mac': {'fixture'}}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sios-backup-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.store = Store(self.base / 'data', thread_projects=self.PROJECTS,
                           thread_principals=self.GRANTS)
        (self.store.root / '.aios-instance.json').write_text(json.dumps({
            'schema_version': 1, 'instance_id': str(uuid.uuid4())}))

    def test_mission_attachments_and_journal_survive_isolated_restore(self):
        (self.store.root / 'Guidance').mkdir()
        (self.store.root / 'Guidance' / 'index.json').write_text(json.dumps({
            'version': 1, 'documents': [{'path': 'AGENTS.md', 'when': 'synthetic work'}]}))
        (self.store.root / 'AGENTS.md').write_text('# Synthetic guidance\n')
        legacy = self.store.root / 'Memory' / 'legacy note 2026-10-20.md'
        legacy.parent.mkdir()
        legacy.write_text('# Synthetic legacy memory\n')
        mission = self.store.root / 'Missions' / 'synthetic' / 'evidence'
        mission.mkdir(parents=True)
        samples = {
            'sample.csv': b'row,value\n1,synthetic\n',
            'sample.json': b'{"synthetic":true}',
            'sample.log': b'synthetic log\n',
            'sample.pdf': b'%PDF-1.4\nsynthetic fixture\n',
            'sample.png': b'\x89PNG\r\n\x1a\nsynthetic fixture',
        }
        (mission.parent / 'mission.md').write_text('# Synthetic mission\n')
        for name, data in samples.items():
            (mission / name).write_bytes(data)
        with self.store._connection() as db:
            db.execute('INSERT INTO operations (request_id,payload,intent,receipt,complete) '
                       'VALUES (?,?,?,?,1)', ('synthetic-receipt', '{}', '{}', '{}'))
        created = dispatch(self.store, 'thread_create', {
            'project': 'fixture', 'purpose': 'Synthetic backup review',
            'participants': [], 'request_id': 'create-backup-thread', 'max_messages': 12},
            actor='mac', scopes=self.GRANTS['mac'])
        thread_id = created['result']['thread_id']
        dispatch(self.store, 'thread_post', {
            'thread_id': thread_id, 'body': 'Synthetic message',
            'kind': 'status', 'request_id': 'post-backup-thread'},
            actor='mac', scopes=self.GRANTS['mac'])
        key = os.urandom(32)
        archive = self.base / 'snapshot.enc'
        backup(self.store, archive, key)
        recovered_root = self.base / 'recovered'
        restore(archive, recovered_root, key)
        for name, data in samples.items():
            self.assertEqual((recovered_root / 'Missions' / 'synthetic' / 'evidence' / name).read_bytes(), data)
        self.assertEqual((recovered_root / 'Guidance' / 'index.json').read_bytes(),
                         (self.store.root / 'Guidance' / 'index.json').read_bytes())
        self.assertEqual((recovered_root / 'AGENTS.md').read_bytes(), b'# Synthetic guidance\n')
        self.assertEqual((recovered_root / 'Memory' / legacy.name).read_bytes(), legacy.read_bytes())
        with sqlite3.connect(recovered_root / 'Database' / 'state') as db:
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('SELECT count(*) FROM operations WHERE request_id=?',
                                        ('synthetic-receipt',)).fetchone()[0], 1)
        recovered = Store(recovered_root, thread_projects=self.PROJECTS,
                          thread_principals=self.GRANTS)
        messages = dispatch(recovered, 'thread_read', {'thread_id': thread_id},
                            actor='mac', scopes=self.GRANTS['mac'])['messages']
        self.assertEqual([item['body'] for item in messages], ['Synthetic message'])

    def test_unsafe_mission_attachment_fails_before_archive_creation(self):
        mission = self.store.root / 'Missions' / 'synthetic'
        mission.mkdir(parents=True)
        (mission / 'Bad Name.PDF').write_bytes(b'synthetic')
        archive = self.base / 'snapshot.enc'
        with self.assertRaises(ValidationError):
            backup(self.store, archive, os.urandom(32))
        self.assertFalse(archive.exists())

    def test_registered_brain_guidance_survives_encrypted_restore(self):
        guidance = (
            'Brain/AGENTS.md',
            'Brain/workflows/review-candidates.md',
            'Brain/workflows/promote-memory.md',
        )
        (self.store.root / 'Guidance').mkdir()
        (self.store.root / 'Guidance' / 'index.json').write_text(json.dumps({
            'version': 1,
            'documents': [{'path': path, 'when': 'synthetic Brain review'} for path in guidance],
        }))
        for path in guidance:
            target = self.store.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f'# Synthetic guidance: {path}\n')
        key = os.urandom(32)
        archive = self.base / 'brain-guidance.enc'
        backup(self.store, archive, key)
        recovered_root = self.base / 'brain-guidance-recovered'
        restore(archive, recovered_root, key)
        for path in guidance:
            self.assertEqual((recovered_root / path).read_bytes(),
                             (self.store.root / path).read_bytes())


if __name__ == '__main__':
    unittest.main()
