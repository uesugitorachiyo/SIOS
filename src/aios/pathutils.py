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
