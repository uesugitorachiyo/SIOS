"""Capacity admission checks without allocating a large archive."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aios.backup import (
    LARGE_ARCHIVE_BYTES, MAX_ARCHIVE_BYTES, MIN_LARGE_ARCHIVE_AVAILABLE_BYTES,
    _admit_large_archive, _read_bounded,
)
from aios.store import ValidationError


class BackupCapacityTests(unittest.TestCase):
    def test_one_gib_archive_boundary(self):
        self.assertEqual(MAX_ARCHIVE_BYTES, 1024 * 1024 * 1024)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'oversized'
            with path.open('wb') as stream:
                stream.truncate(MAX_ARCHIVE_BYTES + 1)
            with self.assertRaisesRegex(ValidationError, '1 GiB'):
                _read_bounded(path, MAX_ARCHIVE_BYTES)

    def test_large_archive_memory_boundary(self):
        with patch('psutil.virtual_memory', return_value=SimpleNamespace(
                available=MIN_LARGE_ARCHIVE_AVAILABLE_BYTES - 1)):
            _admit_large_archive(LARGE_ARCHIVE_BYTES)
            with self.assertRaisesRegex(ValidationError, 'Insufficient available memory'):
                _admit_large_archive(LARGE_ARCHIVE_BYTES + 1)
        with patch('psutil.virtual_memory', return_value=SimpleNamespace(
                available=MIN_LARGE_ARCHIVE_AVAILABLE_BYTES)):
            _admit_large_archive(512 * 1024 * 1024)
        with patch('psutil.virtual_memory', return_value=SimpleNamespace(
                available=MAX_ARCHIVE_BYTES * 8 - 1)):
            with self.assertRaisesRegex(ValidationError, 'Insufficient available memory'):
                _admit_large_archive(MAX_ARCHIVE_BYTES)
        with patch('psutil.virtual_memory', return_value=SimpleNamespace(
                available=MAX_ARCHIVE_BYTES * 8)):
            _admit_large_archive(MAX_ARCHIVE_BYTES)

    def test_unavailable_memory_telemetry_fails_closed(self):
        with patch('psutil.virtual_memory', side_effect=RuntimeError('unavailable')):
            with self.assertRaisesRegex(ValidationError, 'telemetry unavailable'):
                _admit_large_archive(LARGE_ARCHIVE_BYTES + 1)


if __name__ == '__main__':
    unittest.main()
