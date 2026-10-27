"""Native filesystem spelling for validated long paths."""
from __future__ import annotations

import os
from pathlib import Path


def native_path(path):
    """Use the Windows extended-length spelling without changing path content."""
    if os.name != 'nt':
        return Path(path)
    value = os.path.abspath(os.fspath(path))
    if value.startswith('\\\\?\\'):
        return Path(value)
    if value.startswith('\\\\'):
        return Path('\\\\?\\UNC\\' + value[2:])
    return Path('\\\\?\\' + value)


def stable_file_identity(info):
    """Compare the same identity fields for a path and its open handle."""
    identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    # Windows path and open-handle ctime can temporarily disagree after writes.
    return identity if os.name == 'nt' else (*identity, info.st_ctime_ns)
