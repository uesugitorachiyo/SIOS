"""Local instance configuration and guarded storage initialization."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA_VERSION = 1
_MODES = frozenset({"server", "client", "standalone"})
_TOKEN_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_FIELDS = frozenset({"schema_version", "mode", "instance_id", "timezone", "endpoint", "data_root", "cache_root", "token_env"})
_SOURCE_ROOT = Path(__file__).resolve().parents[2]
_DATA_DIRS = (
    "Brain", "Brain/brain", "Brain/inbox", "Brain/sources",
    "Brain/working", "Brain/working/candidates", "Memory", "Missions",
    "Runs", "Database", "Logs",
)
_INSTANCE_MARKER = ".aios-instance.json"


def _is_reparse(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _check_outside_source(path: Path) -> None:
    resolved = path.resolve(strict=False)
    if _within(resolved, _SOURCE_ROOT):
        raise ValueError(f"path is inside the AIOS source checkout: {path}")
    for ancestor in (resolved, *resolved.parents):
        if (ancestor / ".git").exists() or (ancestor / "src" / "aios").exists():
            raise ValueError(f"path is inside a source checkout: {path}")


def _default_roots(instance_id: str) -> tuple[Path, Path]:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "SIOS" / instance_id
        return base / "data", base / "cache"
    if sys.platform == "darwin":
        return (
            Path.home() / "Library" / "Application Support" / "SIOS" / instance_id,
            Path.home() / "Library" / "Caches" / "SIOS" / instance_id,
        )
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return data / "sios" / instance_id, cache / "sios" / instance_id


def _storage_path(raw: str, config_path: Path, default: Path) -> Path:
    if raw == "AUTO":
        candidate = default
    elif isinstance(raw, str) and raw:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = config_path.parent / candidate
    else:
        raise ValueError("storage root must be a nonempty path or AUTO")
    candidate = candidate.absolute()
    # Normalize parent aliases and relative ``..`` without resolving the root
    # itself, which must still be inspected for a symlink or reparse point.
    candidate = candidate.parent.resolve(strict=False) / candidate.name
    _check_outside_source(candidate)
    if candidate.exists() or candidate.is_symlink():
        if _is_reparse(candidate):
            raise ValueError(f"storage root is a link or reparse point: {candidate}")
        if not candidate.is_dir():
            raise ValueError(f"storage root is not a directory: {candidate}")
        _check_tree(candidate)
    return candidate


def _validate_endpoint(endpoint: object, mode: str) -> str | None:
    if endpoint is None:
        if mode == "client":
            raise ValueError("client mode requires endpoint")
        return None
    if not isinstance(endpoint, str) or not endpoint or endpoint.strip() != endpoint or any(c.isspace() for c in endpoint):
        raise ValueError("invalid endpoint")
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid endpoint") from exc
    if parsed.scheme not in ("http", "https") or not hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("invalid endpoint")
    if parsed.netloc.endswith(":") or (port is not None and not 1 <= port <= 65535):
        raise ValueError("invalid endpoint port")
    if parsed.scheme == "http":
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = hostname.rstrip(".").lower() == "localhost"
        if not loopback:
            raise ValueError("HTTP endpoint must be loopback")
    return endpoint


@dataclass(frozen=True)
class Config:
    mode: str
    instance_id: str
    data_root: Path
    cache_root: Path
    timezone: str
    endpoint: str | None
    config_path: Path
    token_env: str = "SIOS_AUTH_TOKEN"

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        config_path = Path(path).absolute()
        _check_outside_source(config_path)
        if _is_reparse(config_path):
            raise ValueError("config file must not be a link or reparse point")
        with config_path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
        if not isinstance(document, dict) or set(document) - _FIELDS or document.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported or invalid config schema")
        mode = document.get("mode")
        if mode not in _MODES:
            raise ValueError("invalid mode")
        try:
            instance_id = str(uuid.UUID(document["instance_id"]))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError("invalid instance_id") from exc
        if document["instance_id"] != instance_id:
            raise ValueError("instance_id must be canonical UUID")
        timezone = document.get("timezone", "UTC")
        try:
            if not isinstance(timezone, str) or not timezone or timezone.startswith("/") or ".." in timezone:
                raise ValueError("invalid timezone")
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("invalid IANA timezone") from exc
        token_env = document.get("token_env", "SIOS_AUTH_TOKEN")
        if not isinstance(token_env, str) or not _TOKEN_ENV.fullmatch(token_env):
            raise ValueError("invalid token_env")
        default_data, default_cache = _default_roots(instance_id)
        data_root = _storage_path(document.get("data_root", "AUTO"), config_path, default_data)
        cache_root = _storage_path(document.get("cache_root", "AUTO"), config_path, default_cache)
        if _within(data_root.resolve(), cache_root.resolve()) or _within(cache_root.resolve(), data_root.resolve()):
            raise ValueError("data_root and cache_root must be separate")
        endpoint = _validate_endpoint(document.get("endpoint"), mode)
        return cls(mode, instance_id, data_root, cache_root, timezone, endpoint, config_path, token_env)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION, "mode": self.mode, "instance_id": self.instance_id,
            "timezone": self.timezone, "endpoint": self.endpoint,
            "data_root": str(self.data_root), "cache_root": str(self.cache_root), "token_env": self.token_env,
        }


def create_config(
    path: str | Path, mode: str, data_root: str | Path | None = None,
    timezone: str = "UTC", endpoint: str | None = None, instance_id: str | None = None,
    token_env: str = "SIOS_AUTH_TOKEN",
    cache_root: str | Path | None = None,
) -> Config:
    """Create a new config exclusively; existing files are never replaced."""
    config_path = Path(path).absolute()
    _check_outside_source(config_path)
    if mode not in _MODES:
        raise ValueError("invalid mode")
    if mode == "client" and instance_id is None:
        raise ValueError("client mode requires the server instance_id")
    uid = str(uuid.uuid4()) if instance_id is None else str(uuid.UUID(instance_id))
    document = {
        "schema_version": SCHEMA_VERSION, "mode": mode, "instance_id": uid,
        "timezone": timezone, "endpoint": endpoint,
        "data_root": str(data_root) if data_root is not None else "AUTO",
        "cache_root": str(cache_root) if cache_root is not None else "AUTO", "token_env": token_env,
    }
    # Validate all fields before writing. This also checks source boundaries.
    _validate_endpoint(endpoint, mode)
    if not isinstance(token_env, str) or not _TOKEN_ENV.fullmatch(token_env):
        raise ValueError("invalid token_env")
    try:
        ZoneInfo(timezone)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("invalid IANA timezone") from exc
    default_data, default_cache = _default_roots(uid)
    validated_data = _storage_path(document["data_root"], config_path, default_data)
    validated_cache = _storage_path(document["cache_root"], config_path, default_cache)
    if _within(validated_data.resolve(), validated_cache.resolve()) or _within(validated_cache.resolve(), validated_data.resolve()):
        raise ValueError("data_root and cache_root must be separate")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(config_path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        config_path.unlink(missing_ok=True)
        raise
    return Config.load(config_path)


def _check_tree(root: Path) -> None:
    if not root.exists():
        return
    if _is_reparse(root):
        raise ValueError(f"storage root is a link or reparse point: {root}")
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            entry = Path(current) / name
            if _is_reparse(entry):
                raise ValueError(f"storage tree contains a link or reparse point: {entry}")


def _bind_instance(root: Path, instance_id: str) -> None:
    marker = root / _INSTANCE_MARKER
    if marker.exists() or marker.is_symlink():
        if _is_reparse(marker):
            raise ValueError("instance marker must not be a link or reparse point")
        try:
            with marker.open("r", encoding="utf-8") as handle:
                saved = json.load(handle)
        except (OSError, ValueError) as exc:
            raise ValueError("invalid instance marker") from exc
        if saved != {"schema_version": SCHEMA_VERSION, "instance_id": instance_id}:
            raise ValueError("data root belongs to a different or invalid instance")
        return
    if any(root.iterdir()):
        raise ValueError("existing data root has no instance marker")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(marker, flags, 0o600)
    except FileExistsError:
        _bind_instance(root, instance_id)
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"schema_version": SCHEMA_VERSION, "instance_id": instance_id}, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        marker.unlink(missing_ok=True)
        raise


def initialize(cfg: Config) -> dict[str, object]:
    """Create empty canonical storage directories for an authoritative instance."""
    if cfg.mode == "client":
        return {"mode": cfg.mode, "created": (), "data_root": cfg.data_root, "cache_root": cfg.cache_root}
    if cfg.mode not in ("server", "standalone"):
        raise ValueError("invalid mode")
    for root in (cfg.data_root, cfg.cache_root):
        _check_outside_source(root)
        _check_tree(root)
    cfg.data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _bind_instance(cfg.data_root, cfg.instance_id)
    for root in (cfg.data_root, cfg.cache_root):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _check_tree(root)
    created: list[Path] = []
    for name in _DATA_DIRS:
        child = cfg.data_root / name
        if not child.exists():
            child.mkdir()
            created.append(child)
        elif _is_reparse(child) or not child.is_dir():
            raise ValueError(f"invalid canonical directory: {child}")
    return {"mode": cfg.mode, "created": tuple(created), "data_root": cfg.data_root, "cache_root": cfg.cache_root}
