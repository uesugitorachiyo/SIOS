"""The Memory gateway reaches oversized Mission logs only through revision and snapshot reads."""

import hashlib
import tempfile
import unittest
from pathlib import Path

from aios.client import RemoteError
from aios.memory_mcp import MemoryGateway
from aios.service import dispatch
from aios.store import MAX_BYTES, Store, ValidationError
from mcp.server.mcpserver.exceptions import ToolError


class InProcessClient:
    """Dispatch to an isolated Store with the server's error codes for these operations."""

    def __init__(self, store):
        self.store = store

    def call(self, operation, params):
        try:
            return dispatch(self.store, operation, params, actor='fixture', scopes={'read'})
        except PermissionError:
            raise RemoteError('forbidden', 403) from None
        except (ValidationError, UnicodeError, TypeError):
            raise RemoteError('invalid_request', 400) from None
        except FileNotFoundError:
            raise RemoteError('not_found', 404) from None


class LargeLogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'data'
        self.mission = self.root / 'Missions/example'
        self.mission.mkdir(parents=True)
        self.gateway = MemoryGateway(InProcessClient(Store(self.root)))

    def read(self, relative):
        return self.gateway.invoke('read', {'relative': relative})

    def test_full_read_limit_is_exact(self):
        (self.mission / 'log.md').write_bytes(b'x' * MAX_BYTES)
        self.assertEqual(len(self.read('Missions/example/log.md')['text']), MAX_BYTES)
        (self.mission / 'log.md').write_bytes(b'x' * (MAX_BYTES + 1))
        with self.assertRaisesRegex(ToolError, 'invalid_request'):
            self.read('Missions/example/log.md')

    def test_oversized_log_uses_revision_and_snapshot_parts(self):
        data = b'x' * (MAX_BYTES + 1)
        (self.mission / 'log.md').write_bytes(data)
        source = hashlib.sha256(data).hexdigest()
        snapshot = self.mission / f'evidence/log-read-snapshot-{source[:12]}'
        snapshot.mkdir(parents=True)
        (snapshot / 'index.md').write_text(f'source_sha256: {source}\nparts: part-001.md, part-002.md\n')
        (snapshot / 'part-001.md').write_bytes(data[:MAX_BYTES])
        (snapshot / 'part-002.md').write_bytes(data[MAX_BYTES:])

        revision = self.gateway.invoke('read_revision', {'relative': 'Missions/example/log.md'})
        self.assertEqual(revision, {'path': 'Missions/example/log.md', 'revision': source,
                                    'bytes': len(data), 'content_returned': False})
        prefix = f'Missions/example/evidence/log-read-snapshot-{source[:12]}/'
        self.assertIn(f'source_sha256: {revision["revision"]}', self.read(prefix + 'index.md')['text'])
        parts = [self.read(prefix + name)['text'] for name in ('part-001.md', 'part-002.md')]
        self.assertEqual(''.join(parts).encode(), data)

        # A later log append makes the snapshot stale; the revision no longer matches.
        with (self.mission / 'log.md').open('ab') as stream:
            stream.write(b'y')
        later = self.gateway.invoke('read_revision', {'relative': 'Missions/example/log.md'})
        self.assertNotEqual(later['revision'], source)
        self.assertEqual(later['bytes'], len(data) + 1)

    def test_missing_log_revision_is_not_found(self):
        with self.assertRaisesRegex(ToolError, 'not_found'):
            self.gateway.invoke('read_revision', {'relative': 'Missions/example/log.md'})


if __name__ == '__main__':
    unittest.main()
