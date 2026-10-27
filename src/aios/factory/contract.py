"""Closed input, path, and evidence validation."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
MAX_RECORD = 2 * 1024 * 1024

class Blocked(RuntimeError):
    state = "blocked"

    def __init__(self, code, evidence_refs=()):
        super().__init__(code)
        self.code, self.evidence_refs = code, list(evidence_refs)

def require(condition, code):
    if not condition:
        raise Blocked(code)

def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def strict_json(raw: bytes):
    require(type(raw) is bytes and len(raw) <= MAX_RECORD, "record_size")
    def pairs(items):
        out = {}
        for k, v in items:
            require(k not in out, "duplicate_key")
            out[k] = v
        return out
    def bad(value):
        raise Blocked("nonfinite_number")
    def finite_float(value):
        import math
        number = float(value)
        require(math.isfinite(number), "nonfinite_number")
        return number
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=bad, parse_float=finite_float)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Blocked("invalid_json") from exc

def relative(value):
    require(type(value) is str and 0 < len(value) <= 4096, "invalid_path")
    parts = value.split("/")
    require(not value.startswith("/") and all(p not in ("", ".", "..") for p in parts), "invalid_path")
    require("\\" not in value and ":" not in value and "\0" not in value, "invalid_path")
    require(str(PurePosixPath(value)) == value, "invalid_path")
    # Portable names avoid NTFS streams, device aliases and case collisions.
    for p in parts:
        require(not p.endswith((" ", ".")), "path_alias")
        require(not re.fullmatch(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", p, re.I), "path_alias")
    return value

def plain(path: Path, *, directory=False):
    require(".." not in Path(path).parts, "path_traversal")
    path = Path(path).absolute()
    if os.name == "nt":
        from .platforms._windows_support import validate_native_path
        validate_native_path(path)
    for part in (*reversed(path.parents), path):
        if part.exists() or part.is_symlink():
            s = part.lstat()
            require(not stat.S_ISLNK(s.st_mode) and not getattr(s, "st_file_attributes", 0) & 0x400, "linked_path")
    if path.exists():
        s = path.stat()
        require(stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode), "wrong_file_type")
        if not directory:
            require(s.st_nlink == 1, "hardlinked_file")
    return path

def below(root, name):
    relative(name)
    root = plain(Path(root), directory=True)
    path = root.joinpath(*name.split("/"))
    plain(path, directory=path.is_dir())
    require(path.resolve().is_relative_to(root.resolve()), "escaped_root")
    return path

def utc(value):
    require(type(value) is str and value.endswith("Z"), "invalid_utc")
    try:
        out = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise Blocked("invalid_utc") from exc
    require(out.utcoffset().total_seconds() == 0, "invalid_utc")
    return out
