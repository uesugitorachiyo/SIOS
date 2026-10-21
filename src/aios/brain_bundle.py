"""Reviewed Brain concept + containing index + log change.

This preserves the existing one-document Brain API. Bundle proposals are
separate immutable journaled records; apply requires an authenticated owner
scope and the exact reviewed aggregate revision.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import difflib
import hashlib
import json
import re
import sqlite3
from pathlib import PurePosixPath

from .brain import BrainWorkflow, _proposal_id, _verification_unchanged
from .store import Store, ConflictError, NotFoundError, ValidationError, _hash, _json, _text


def _paths(target: str) -> tuple[str, str]:
    parts = PurePosixPath(target)
    if parts.parts[:2] != ("Brain", "brain") or parts.suffix != ".md" or parts.name in {"index.md", "log.md"}:
        raise ValidationError("Bundle target must be a Brain concept")
    return (str(parts.parent / "index.md"), "Brain/brain/log.md")


def _check_index(target: str, text: str) -> None:
    _text(text, "containing index")
    index_path, _ = _paths(target)
    if index_path == "Brain/brain/index.md":
        if not re.match(r'\A---\r?\nokf_version:\s*["\']?0\.2["\']?\r?\n---\r?\n', text):
            raise ValidationError("Root Brain index requires OKF 0.2 frontmatter")
    elif text.startswith("---\n"):
        raise ValidationError("Nested Brain index must not have frontmatter")
    if not re.search(r"(?m)^#\s+\S", text):
        raise ValidationError("Brain index requires a heading")
    link = "/" + target.removeprefix("Brain/brain/")
    if link not in text:
        raise ValidationError("Containing Brain index must link the proposed concept")


def _check_log(old: str, new: str) -> None:
    _text(new, "Brain log")
    if not new.startswith(old) or new == old:
        raise ValidationError("Brain log update must append to and preserve the complete previous log")
    if new.startswith("---\n") or not re.match(r"\A#\s+\S", new):
        raise ValidationError("Brain log requires a title and no frontmatter")
    addition = new[len(old):]
    headings = re.findall(r"(?m)^##\s+([^\n]+)$", new)
    if not re.search(r"(?m)^##\s+\d{4}-\d{2}-\d{2}\s*$", addition):
        raise ValidationError("Brain log append needs a dated entry")
    for heading in headings:
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", heading):
                raise ValueError()
            dt.date.fromisoformat(heading)
        except ValueError as exc:
            raise ValidationError("Brain log headings require valid ISO dates") from exc


class BrainBundleWorkflow:
    def __init__(self, store: Store):
        self.store = store
        self.concepts = BrainWorkflow(store)

    def propose(self, concept_proposal_id: str, index_content: str, log_content: str,
                index_expected_revision: str, log_expected_revision: str,
                request_id: str, actor: str = "local"):
        concept_proposal_id = _proposal_id(concept_proposal_id)
        _text(request_id, "request_id", 200)
        for value in (index_expected_revision, log_expected_revision):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValidationError("Index and log require exact existing SHA-256 revisions")
        bundle_id = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        meta_path = f"Runs/brain-bundles/{bundle_id}.json"
        index_candidate = f"Brain/working/candidates/{bundle_id}-index.md"
        log_candidate = f"Brain/working/candidates/{bundle_id}-log.md"

        def build():
            concept, content = self.concepts._load(concept_proposal_id)
            target = concept["target"]
            index_path, log_path = _paths(target)
            if self.store._bytes(meta_path) is not None or any(self.store._bytes(p) is not None for p in (index_candidate, log_candidate)):
                raise ConflictError("Brain bundle proposal already exists")
            current = self.store._bytes(target)
            index_old = self.store._bytes(index_path)
            log_old = self.store._bytes(log_path)
            if _hash(current) != concept["base_revision"] or _hash(index_old) != index_expected_revision or _hash(log_old) != log_expected_revision:
                raise ConflictError("Brain concept, index or log changed; reread")
            if index_old is None or log_old is None:
                raise NotFoundError("Brain index or log is missing")
            _check_index(target, index_content)
            _check_log(log_old.decode("utf-8"), log_content)
            binding = {"concept_proposal_id": concept_proposal_id,
                       "concept_revision": concept["proposal_revision"],
                       "target": target, "index_path": index_path, "log_path": log_path,
                       "index_base_revision": index_expected_revision,
                       "log_base_revision": log_expected_revision,
                       "index_revision": _hash(index_content.encode()),
                       "log_revision": _hash(log_content.encode())}
            bundle_revision = _hash(_json(binding).encode())
            metadata = {"schema_version": 1, "bundle_id": bundle_id,
                        "request_id": request_id, "binding": binding,
                        "index_candidate": index_candidate, "log_candidate": log_candidate,
                        "bundle_revision": bundle_revision, "proposed_by": actor,
                        "created_at": dt.datetime.now(dt.timezone.utc).isoformat()}
            result = {"bundle_id": bundle_id, "bundle_revision": bundle_revision,
                      "concept_proposal_id": concept_proposal_id, "target": target,
                      "index_path": index_path, "log_path": log_path}
            return [(index_candidate, index_content), (log_candidate, log_content),
                    (meta_path, _json(metadata) + "\n")], result

        return self.store._operation(request_id, actor,
             {"operation": "brain_bundle_propose", "concept_proposal_id": concept_proposal_id,
              "index_content": index_content, "log_content": log_content,
              "index_expected_revision": index_expected_revision,
              "log_expected_revision": log_expected_revision}, build)

    def _load(self, bundle_id: str):
        bundle_id = _proposal_id(bundle_id)
        meta_path = f"Runs/brain-bundles/{bundle_id}.json"
        raw = self.store._bytes(meta_path)
        if raw is None:
            raise NotFoundError(bundle_id)
        try:
            metadata = json.loads(raw)
            if metadata["schema_version"] != 1 or metadata["bundle_id"] != bundle_id:
                raise ValueError()
            if hashlib.sha256(metadata["request_id"].encode()).hexdigest() != bundle_id:
                raise ValueError()
            with contextlib.closing(sqlite3.connect(self.store._path("Database/state"))) as db:
                intent = db.execute("SELECT intent FROM operations WHERE request_id=?", (metadata["request_id"],)).fetchone()
            if intent is None or len([i for i in json.loads(intent[0]) if i["path"] == meta_path and i["text"].encode() == raw]) != 1:
                raise ValueError()
            binding = metadata["binding"]
            concept, content = self.concepts._load(binding["concept_proposal_id"])
            if concept["proposal_revision"] != binding["concept_revision"] or concept["target"] != binding["target"]:
                raise ValueError()
            if _paths(binding["target"]) != (binding["index_path"], binding["log_path"]):
                raise ValueError()
            if metadata["index_candidate"] != f"Brain/working/candidates/{bundle_id}-index.md" or metadata["log_candidate"] != f"Brain/working/candidates/{bundle_id}-log.md":
                raise ValueError()
            index_data = self.store._bytes(metadata["index_candidate"])
            log_data = self.store._bytes(metadata["log_candidate"])
            if index_data is None or log_data is None or _hash(index_data) != binding["index_revision"] or _hash(log_data) != binding["log_revision"]:
                raise ValueError()
            if _hash(_json(binding).encode()) != metadata["bundle_revision"]:
                raise ValueError()
            index_content, log_content = index_data.decode(), log_data.decode()
            _check_index(binding["target"], index_content)
            old_log = self.store._bytes(binding["log_path"])
            if old_log is not None and _hash(old_log) == binding["log_base_revision"]:
                _check_log(old_log.decode(), log_content)
            _verification_unchanged(content, concept["base_content"])
        except (KeyError, TypeError, ValueError, UnicodeError, AttributeError) as exc:
            raise ConflictError("Brain bundle metadata or candidate changed") from exc
        return metadata, concept, content, index_content, log_content

    def review(self, bundle_id: str):
        with self.store._connection() as db:
            self.store._recover(db)
            metadata, concept, content, index_content, log_content = self._load(bundle_id)
            b = metadata["binding"]
            index_old = self.store._bytes(b["index_path"])
            log_old = self.store._bytes(b["log_path"])
            current = self.store._bytes(b["target"])
            stale = (_hash(current) != concept["base_revision"] or
                     _hash(index_old) != b["index_base_revision"] or
                     _hash(log_old) != b["log_base_revision"])
            def diff(old, new, path):
                return "".join(difflib.unified_diff((old or "").splitlines(keepends=True), new.splitlines(keepends=True),
                                                     fromfile=path, tofile=path + ".proposed"))
            return {"bundle_id": bundle_id, "bundle_revision": metadata["bundle_revision"],
                    "target": b["target"], "stale": stale,
                    "concept_diff": diff(concept["base_content"], content, b["target"]),
                    "index_diff": diff(index_old.decode() if index_old else None, index_content, b["index_path"]),
                    "log_diff": diff(log_old.decode() if log_old else None, log_content, b["log_path"]),
                    "warnings": ["Owner must verify semantics, provenance, sensitivity and link correctness before approval."]}

    def apply(self, bundle_id: str, bundle_revision: str, request_id: str,
              actor: str, owner_authorized: bool = False):
        if owner_authorized is not True:
            raise PermissionError("Brain bundle changes require authenticated owner approval")
        _text(actor, "owner actor", 200)
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", actor):
            raise ValidationError("Owner actor must be a device slug")
        bundle_id = _proposal_id(bundle_id)
        if not isinstance(bundle_revision, str) or not re.fullmatch(r"[0-9a-f]{64}", bundle_revision):
            raise ValidationError("Approval requires the reviewed bundle SHA-256")
        audit_path = f"Runs/brain-bundle-approvals/{bundle_id}.json"

        def build():
            metadata, concept, content, index_content, log_content = self._load(bundle_id)
            b = metadata["binding"]
            if bundle_revision != metadata["bundle_revision"]:
                raise ConflictError("Approval differs from reviewed Brain bundle")
            if self.store._bytes(audit_path) is not None:
                raise ConflictError("Brain bundle already applied; replay original request ID")
            for path, base in ((b["target"], concept["base_revision"]),
                               (b["index_path"], b["index_base_revision"]),
                               (b["log_path"], b["log_base_revision"])):
                if _hash(self.store._bytes(path)) != base:
                    raise ConflictError("Brain bundle base changed; prepare a new proposal")
            _check_log(self.store._bytes(b["log_path"]).decode(), log_content)
            audit = {"schema_version": 1, "event": "human-confirmed", "actor": f"human:{actor}",
                     "bundle_id": bundle_id, "bundle_revision": bundle_revision,
                     "request_id": request_id, "confirmed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                     "binding": b, "scope": "exact concept, index and log bytes only"}
            result = {"bundle_id": bundle_id, "bundle_revision": bundle_revision,
                      "target": b["target"], "revision": b["concept_revision"], "audit_path": audit_path}
            return [(b["target"], content), (b["index_path"], index_content),
                    (b["log_path"], log_content), (audit_path, _json(audit) + "\n")], result

        return self.store._operation(request_id, actor,
             {"operation": "brain_bundle_apply", "bundle_id": bundle_id,
              "bundle_revision": bundle_revision}, build)
