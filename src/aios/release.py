"""Inactive, synthetic protected release-eligibility validation.

This module never publishes assets or creates production Ready/Released facts.
It reuses the Store journal and authenticated adapter principals. The trusted
host/filesystem remains outside the remote-principal protection boundary.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import gzip
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import struct
import sys
import tarfile
import zipfile

from . import completion, verification as v
from .store import ConflictError, MAX_BYTES, ValidationError, _hash, _json, _text

BLOCK = "release consumer/approvals/artifact qualification is outside advisory pilot"
LIMIT = 32 * 1024 * 1024
MEMBER_LIMIT = 5000
ZIP_METADATA_LIMIT = 4 * 1024 * 1024
ZIP_READ_CHUNK = 64 * 1024
TAR_EXPANDED_LIMIT = LIMIT + 16 * 1024 * 1024
TAR_PAX_ENTRY_LIMIT = 1024 * 1024
TAR_PAX_TOTAL_LIMIT = 4 * 1024 * 1024
TAR_PAX_HEADER_LIMIT = 256
TAR_PAX_CHAIN_LIMIT = 8


def now():
    return datetime.now(timezone.utc)


def require(store):
    if not store.release_validation:
        raise ValidationError("Release validation instance is disabled")


def initialize(db):
    db.execute("CREATE TABLE IF NOT EXISTS release_controls(id TEXT PRIMARY KEY, actor TEXT NOT NULL, control TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)")
    db.execute("CREATE TABLE IF NOT EXISTS release_evidence(id TEXT NOT NULL, reference TEXT NOT NULL, actor TEXT NOT NULL, record TEXT NOT NULL, PRIMARY KEY(id,reference))")
    db.execute("CREATE TABLE IF NOT EXISTS release_reports(id TEXT NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(id,kind))")
    db.execute("CREATE TABLE IF NOT EXISTS release_runs(operation_id TEXT PRIMARY KEY, verification_id TEXT UNIQUE NOT NULL, security_run_id TEXT UNIQUE NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL)")


def encoded(value):
    text = v.canonical(value).decode("utf-8")
    _text(text, "release record", MAX_BYTES)
    return text


def control_row(db, vid):
    row = db.execute("SELECT control,actor,revoked FROM release_controls WHERE id=?", (vid,)).fetchone()
    if row is None:
        raise ValidationError("Retained release control unavailable")
    if row[2]:
        raise PermissionError("Release control revoked")
    return json.loads(row[0]), row[1]


def _valid_profile(control):
    profile = control["profile"]
    v.exact(profile, {"version", "platforms", "channel", "audience", "owner", "stop_ref",
                      "recovery_ref", "provenance_ref", "platform_refs"})
    for key in ("version", "channel", "audience", "owner"):
        v.opaque(profile[key])
    for key in ("stop_ref", "recovery_ref", "provenance_ref"):
        v.opaque(profile[key])
    platforms = profile["platforms"]
    if (not isinstance(platforms, list) or not platforms or len(set(platforms)) != len(platforms)
            or set(platforms) - {"macos", "windows", "linux"}):
        raise ValidationError("Invalid required release platforms")
    v.exact(profile["platform_refs"], set(platforms))
    for ref in profile["platform_refs"].values():
        v.opaque(ref)
    # The approved profile file is an exact canonical representation.
    if v.digest(profile) != control["request"]["binding"]["profile_sha256"]:
        raise ValidationError("Release profile and bound profile file differ")


def _valid_artifacts(control):
    artifacts = control["artifacts"]
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 8:
        raise ValidationError("Release artifact set missing or oversized")
    names = []
    for item in artifacts:
        v.exact(item, {"path", "size_bytes", "sha256", "format", "members"})
        names.append(v.path_name(item["path"]))
        if item["format"] not in {"zip", "tar.gz"} or not v.SHA.fullmatch(item["sha256"]):
            raise ValidationError("Unsupported artifact identity")
        if type(item["size_bytes"]) is not int or not 0 < item["size_bytes"] <= LIMIT:
            raise ValidationError("Invalid artifact size")
        members = item["members"]
        if not isinstance(members, list) or not members or len(members) > MEMBER_LIMIT:
            raise ValidationError("Artifact inventory missing or oversized")
        inner_names = []
        for member in members:
            v.exact(member, {"path", "size_bytes", "sha256"})
            inner_names.append(v.path_name(member["path"]))
            if (type(member["size_bytes"]) is not int or not 0 <= member["size_bytes"] <= LIMIT
                    or not v.SHA.fullmatch(member["sha256"])):
                raise ValidationError("Invalid inner inventory member")
        if inner_names != sorted(set(inner_names)) or len({n.casefold() for n in inner_names}) != len(inner_names):
            raise ValidationError("Inner inventory is not unique and sorted")
    if names != sorted(set(names)) or len({n.casefold() for n in names}) != len(names):
        raise ValidationError("Artifact inventory is not unique and sorted")
    if v.digest(artifacts) != control["request"]["binding"]["artifact_sha256"]:
        raise ValidationError("Artifact manifest binding differs")


def validate_control(control, actor):
    v.exact(control, {"schema_version", "request", "candidate_root", "candidate_files",
                      "candidate_exclusions", "bound_files", "artifact_root", "artifacts",
                      "profile", "checker", "report_actors", "receipt_producers", "approval_actor"})
    if control["schema_version"] != "1.0":
        raise ValidationError("Unknown release control version")
    v.opaque(actor)
    route = v.validate_request(control["request"])
    if route["route"] != "PROTECTED":
        raise ValidationError("Release qualification requires protected routing")
    b = control["request"]["binding"]
    if b["stage"] != "release" or b["artifact_sha256"] is None:
        raise ValidationError("Exact release artifact binding required")
    v.path_name(control["candidate_root"])
    v.path_name(control["artifact_root"])
    if control["candidate_root"] == control["artifact_root"]:
        raise ValidationError("Artifact and source roots must differ")
    _valid_profile(control)
    _valid_artifacts(control)
    v.opaque(control["checker"])
    v.opaque(control["approval_actor"])
    v.exact(control["report_actors"], {"qa", "security"})
    for person in control["report_actors"].values():
        v.opaque(person)
    if control["approval_actor"] in {actor, control["checker"], *control["report_actors"].values()}:
        raise PermissionError("Release approval must have independent principal")
    if not isinstance(control["receipt_producers"], dict) or not control["receipt_producers"]:
        raise ValidationError("Receipt producers missing")
    for reference, producer in control["receipt_producers"].items():
        v.opaque(reference)
        v.exact(producer, {"actor", "session", "kind"})
        v.opaque(producer["actor"])
        v.opaque(producer["session"])
        if producer["kind"] not in {"context", "mechanical", "automated", "independent_review"}:
            raise ValidationError("Invalid receipt producer kind")
    if control["request"]["reviewer_actor"] != control["report_actors"]["qa"] and v.classify(
            control["request"]["assessment"], control["request"]["route_control"],
            control["request"]["requirements"])["reviewer_dispatch"] == "REQUIRED":
        raise ValidationError("Review actor mismatch")


def _stable_file(path: Path):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValidationError("Artifact is not a regular file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValidationError("Artifact changed during open")
        data = stream.read(LIMIT + 1)
        after = os.fstat(stream.fileno())
    final = path.lstat()
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode")
    if (len(data) > LIMIT or any(tuple(getattr(s, f) for f in fields) != tuple(getattr(before, f) for f in fields)
                               for s in (opened, after, final))):
        raise ValidationError("Artifact changed during read or exceeds limit")
    return data


def _zip_preflight(data):
    # Parse the bounded central-directory envelope before ZipFile allocates ZipInfo
    # objects. ZIP64 and multi-disk archives are outside this fixture contract.
    floor = max(0, len(data) - (65535 + 22))
    marker = b"PK\x05\x06"
    pos = data.rfind(marker, floor)
    if pos < 0 or pos + 22 > len(data):
        raise ValidationError("ZIP end record unavailable")
    _, disk, cd_disk, on_disk, declared, cd_size, cd_offset, comment = struct.unpack_from(
        "<IHHHHIIH", data, pos)
    if (pos + 22 + comment != len(data) or disk or cd_disk or on_disk != declared
            or declared > MEMBER_LIMIT or declared == 0xffff or cd_size > ZIP_METADATA_LIMIT
            or cd_size == 0xffffffff or cd_offset == 0xffffffff
            or cd_offset + cd_size != pos):
        raise ValidationError("Unsupported or oversized ZIP directory")
    cursor, end, count = cd_offset, pos, 0
    while cursor < end:
        if cursor + 46 > end or data[cursor:cursor + 4] != b"PK\x01\x02":
            raise ValidationError("Invalid ZIP central directory")
        packed, unpacked = struct.unpack_from("<II", data, cursor + 20)
        name_len, extra_len, comment_len, member_disk = struct.unpack_from("<HHHH", data, cursor + 28)
        local_offset = struct.unpack_from("<I", data, cursor + 42)[0]
        if (packed == 0xffffffff or unpacked == 0xffffffff or local_offset == 0xffffffff
                or unpacked > LIMIT or member_disk):
            raise ValidationError("Unsupported ZIP64 or multi-disk member")
        extra = data[cursor + 46 + name_len:cursor + 46 + name_len + extra_len]
        offset = 0
        while offset < len(extra):
            if offset + 4 > len(extra):
                raise ValidationError("Malformed ZIP extra field")
            field, length = struct.unpack_from("<HH", extra, offset)
            if field == 1:
                raise ValidationError("ZIP64 member unsupported")
            offset += 4 + length
            if offset > len(extra):
                raise ValidationError("Malformed ZIP extra field")
        cursor += 46 + name_len + extra_len + comment_len
        count += 1
        if cursor > end or count > MEMBER_LIMIT:
            raise ValidationError("ZIP central directory exceeds budget")
    if cursor != end or count != declared:
        raise ValidationError("ZIP central directory count differs")


def _tar_preflight(data):
    # A compressed 32 MiB input can otherwise expand into unbounded tar/PAX
    # metadata before tarfile.getmembers returns. Bound expansion first, then
    # count raw headers and cap extended-header bytes before tarfile parsing.
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
        expanded = compressed.read(TAR_EXPANDED_LIMIT + 1)
    if len(expanded) > TAR_EXPANDED_LIMIT:
        raise ValidationError("Expanded tar exceeds budget")
    cursor = count = physical = pax_headers = pax_chain = pax_total = 0
    ended = False
    while cursor + 512 <= len(expanded):
        header = expanded[cursor:cursor + 512]
        if not any(header):
            if any(expanded[cursor:]):
                raise ValidationError("Nonzero data after tar end")
            ended = True
            break
        raw_size = header[124:136]
        if raw_size[0] & 0x80:
            raise ValidationError("Unsupported binary tar size")
        try:
            size = int(raw_size.strip(b"\x00 ") or b"0", 8)
        except ValueError as exc:
            raise ValidationError("Invalid tar size") from exc
        kind = header[156:157]
        physical += 1
        if physical > MEMBER_LIMIT:
            raise ValidationError("Tar physical header count exceeds budget")
        if kind in (b"0", b"\x00", b"5"):
            count += 1
            pax_chain = 0
            if count > MEMBER_LIMIT or size > LIMIT or (kind == b"5" and size):
                raise ValidationError("Tar member count or size exceeds budget")
        elif kind in (b"x", b"g"):
            pax_headers += 1
            pax_chain += 1
            pax_total += size
            if (pax_headers > TAR_PAX_HEADER_LIMIT or pax_chain > TAR_PAX_CHAIN_LIMIT
                    or size > TAR_PAX_ENTRY_LIMIT or pax_total > TAR_PAX_TOTAL_LIMIT):
                raise ValidationError("Tar PAX metadata exceeds budget")
        else:
            raise ValidationError("Unsupported tar entry type")
        cursor += 512 + ((size + 511) // 512) * 512
        if cursor > len(expanded):
            raise ValidationError("Truncated tar member")
    if not ended or not count:
        raise ValidationError("Tar end or members unavailable")
    return expanded


def _archive_name(name):
    try:
        return v.path_name(name)
    except ValueError as exc:
        raise ValidationError("Unsafe archive member path") from exc


def _zip_member(archive, member, budget):
    if member.file_size > budget:
        raise ValidationError("Expanded archive exceeds limit")
    if member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        # Older ZipExtFile expands these codecs before clipping to the declared
        # size. A finite caller read alone cannot repair that internal behavior.
        floors = {(3, 11): (3, 11, 17), (3, 12): (3, 12, 15),
                  (3, 13): (3, 13, 16), (3, 14): (3, 14, 8)}
        minimum = floors.get(sys.version_info[:2])
        codecs = {zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA}
        if hasattr(zipfile, "ZIP_ZSTANDARD"):
            codecs.update((20, zipfile.ZIP_ZSTANDARD))
        if member.compress_type not in codecs:
            raise ValidationError("Unsupported ZIP compression")
        if (sys.implementation.name != "cpython" or minimum is None
                or sys.version_info[:3] < minimum or sys.version_info.releaselevel != "final"):
            raise ValidationError("ZIP compression requires a supported patched CPython runtime")
    size = 0
    digest = hashlib.sha256()
    with archive.open(member) as stream:
        while True:
            block = stream.read(min(ZIP_READ_CHUNK, budget - size + 1))
            if not block:
                break
            size += len(block)
            if size > budget:
                raise ValidationError("Expanded archive exceeds limit")
            digest.update(block)
    if size != member.file_size:
        raise ValidationError("Archive member size differs")
    return size, digest.hexdigest()


def _members(item, data):
    found = []
    total = 0
    seen = set()
    if item["format"] == "zip":
        _zip_preflight(data)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for member in archive.infolist():
                mode = (member.external_attr >> 16) & 0o170000
                directory = member.is_dir()
                if (member.flag_bits & 1 or mode not in ((0, stat.S_IFDIR) if directory else (0, stat.S_IFREG))
                        or (directory and member.file_size != 0)):
                    raise ValidationError("Unsafe archive member")
                name = _archive_name(member.filename[:-1] if directory else member.filename)
                if name.casefold() in seen:
                    raise ValidationError("Duplicate archive member")
                seen.add(name.casefold())
                if directory:
                    continue
                if member.file_size > LIMIT:
                    raise ValidationError("Oversized archive member")
                size, digest = _zip_member(archive, member, LIMIT - total)
                total += size
                found.append({"path": name, "size_bytes": size, "sha256": digest})
    else:
        expanded = _tar_preflight(data)
        with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
            for member in archive:
                name = _archive_name(member.name[:-1] if member.isdir() and member.name.endswith("/") else member.name)
                if name.casefold() in seen:
                    raise ValidationError("Duplicate archive member")
                seen.add(name.casefold())
                if member.isdir():
                    continue
                if not member.isfile() or member.size > LIMIT:
                    raise ValidationError("Unsafe archive member")
                body = archive.extractfile(member).read(LIMIT + 1)
                total += len(body)
                if total > LIMIT:
                    raise ValidationError("Expanded archive exceeds limit")
                found.append({"path": name, "size_bytes": len(body), "sha256": _hash(body)})
    if len(found) != len({record["path"].casefold() for record in found}) or sorted(found, key=lambda r: r["path"]) != item["members"]:
        raise ValidationError("Final artifact inner inventory differs")


def current_artifacts(store, control):
    root = store._path(control["artifact_root"])
    if not root.is_dir():
        raise ValidationError("Artifact root unavailable")
    expected = [x["path"] for x in control["artifacts"]]
    actual = []
    total = 0
    def failed_walk(error):
        raise ValidationError("Final artifact inventory incomplete") from error
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=failed_walk):
        for name in dirs:
            store._check_node(Path(directory) / name)
        for name in files:
            path = Path(directory) / name
            store._check_node(path)
            actual.append(v.path_name(path.relative_to(root).as_posix()))
    if sorted(actual) != expected or len({name.casefold() for name in actual}) != len(actual):
        raise ValidationError("Final artifact set differs")
    for item in control["artifacts"]:
        data = _stable_file(store._read_path(f'{control["artifact_root"]}/{item["path"]}'))
        total += len(data)
        if total > LIMIT or len(data) != item["size_bytes"] or _hash(data) != item["sha256"]:
            raise ValidationError("Final artifact bytes differ")
        try:
            _members(item, data)
        except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError) as exc:
            raise ValidationError("Invalid final artifact archive") from exc


def register(store, control, actor):
    require(store)
    validate_control(control, actor)
    body = encoded(control)
    vid = control["request"]["request_id"]
    with store._connection() as db:
        if db.execute("SELECT 1 FROM completion_controls WHERE id=?", (vid,)).fetchone():
            raise ConflictError("Verification ID belongs to completion")
        old = db.execute("SELECT control,actor,revoked FROM release_controls WHERE id=?", (vid,)).fetchone()
        if old:
            if old != (body, actor, 0):
                raise ConflictError("Release control already registered or revoked")
            return {"verification_id": vid, "control_digest": v.digest(control), "validation_instance": True,
                    "enforcement_active": False}
        if completion.current_binding(store, control) != control["request"]["binding"]:
            raise ValidationError("Current release candidate differs")
        current_artifacts(store, control)
        if not v.stamp(control["request"]["issued_at"]) <= now() < v.stamp(control["request"]["expires_at"]):
            raise ValidationError("Release request outside validity")
        db.execute("INSERT INTO release_controls(id,actor,control) VALUES(?,?,?)", (vid, actor, body))
    return {"verification_id": vid, "control_digest": v.digest(control), "validation_instance": True,
            "enforcement_active": False}


def evidence(store, vid, reference, record, actor):
    require(store)
    body = encoded(record)
    with store._connection() as db:
        control, _ = control_row(db, vid)
        producer = control["receipt_producers"].get(reference)
        if not producer or actor != producer["actor"]:
            raise PermissionError("Wrong release receipt actor/reference")
        v.exact(record, {"bytes", "sha256", "binding", "actor", "session", "kind", "count",
                         "observations", "started_at", "finished_at"})
        if (not isinstance(record["bytes"], str) or _hash(record["bytes"].encode()) != record["sha256"]
                or any(record[k] != producer[k] for k in ("actor", "session", "kind"))
                or record["binding"] != control["request"]["binding"]):
            raise ValidationError("Release receipt content or producer differs")
        completion._immutable(db, "release_evidence", ("id", "reference"), (vid, reference), "record", body, actor)
    return {"retained": reference, "authenticated_actor": actor}


def report(store, vid, kind, result, actor):
    require(store)
    body = encoded(result)
    with store._connection() as db:
        control, _ = control_row(db, vid)
        if kind not in control["report_actors"] or actor != control["report_actors"][kind]:
            raise PermissionError("Wrong release report actor")
        if kind == "qa" and result.get("actor") != actor:
            raise ValidationError("QA report actor differs")
        completion._immutable(db, "release_reports", ("id", "kind"), (vid, kind), "result", body, actor)
    return {"retained": kind, "authenticated_actor": actor}


def approve(store, vid, control_digest, expires_at, actor):
    require(store)
    with store._connection() as db:
        control, _ = control_row(db, vid)
        if actor != control["approval_actor"]:
            raise PermissionError("Wrong release approver")
        if (control_digest != v.digest(control) or not now() < v.stamp(expires_at)
                <= v.stamp(control["request"]["expires_at"])):
            raise ValidationError("Release approval target or expiry differs")
        if completion.current_binding(store, control) != control["request"]["binding"]:
            raise ValidationError("Release candidate changed before approval")
        current_artifacts(store, control)
        old = db.execute("SELECT actor,digest,expires FROM completion_approvals WHERE id=?", (vid,)).fetchone()
        if old and old != (actor, control_digest, expires_at):
            raise ConflictError("Release approval immutable")
        db.execute("INSERT OR IGNORE INTO completion_approvals VALUES(?,?,?,?)", (vid, actor, control_digest, expires_at))
    return {"approved_scope": "inactive_release_validation", "validation_instance": True,
            "enforcement_active": False}


def revoke(store, vid, actor):
    require(store)
    with store._connection() as db:
        _, coordinator = control_row(db, vid)
        if actor != coordinator:
            raise PermissionError("Wrong release coordinator")
        db.execute("UPDATE release_controls SET revoked=1 WHERE id=?", (vid,))
    return {"revoked": vid}


def _registry(db, vid):
    records = {ref: json.loads(body) for ref, body in db.execute(
        "SELECT reference,record FROM release_evidence WHERE id=?", (vid,))}
    for record in records.values():
        record["bytes"] = record["bytes"].encode("utf-8")
    return records


def _profile_receipt(registry, ref, binding, *, observations):
    v.opaque(ref)
    record = registry.get(ref)
    if record is None or record["binding"] != binding:
        raise ValidationError("Required release profile evidence missing")
    v.exact(record, {"bytes", "sha256", "binding", "actor", "session", "kind", "count",
                     "observations", "started_at", "finished_at"})
    if (not isinstance(record["bytes"], bytes) or not record["bytes"]
            or _hash(record["bytes"]) != record["sha256"] or type(record["count"]) is not int
            or record["count"] < 1):
        raise ValidationError("Invalid release profile receipt")
    if not set(observations) <= set(record["observations"]):
        raise ValidationError("Release profile observation missing")
    return record


def validate(store, db, vid, actor):
    require(store)
    control, _ = control_row(db, vid)
    if actor != control["checker"]:
        raise PermissionError("Wrong release checker")
    results = {kind: json.loads(body) for kind, body in db.execute(
        "SELECT kind,result FROM release_reports WHERE id=?", (vid,))}
    if set(results) != {"qa", "security"}:
        raise ValidationError("Release QA/Security reports missing")
    registry = _registry(db, vid)
    current = completion.current_binding(store, control)
    current_artifacts(store, control)
    result = v.assess(control["request"], results["qa"], results["security"], registry,
                      current, now=now())
    if result["blockers"] != [BLOCK]:
        raise ValidationError("Release verification blocked: " + "; ".join(result["blockers"]))
    profile = control["profile"]
    for key, obs in (("stop_ref", ["stop-conditions-reviewed"]),
                     ("recovery_ref", ["recovery-rehearsed"]),
                     ("provenance_ref", ["build-provenance-checked"])):
        _profile_receipt(registry, profile[key], current, observations=obs)
    for platform, ref in profile["platform_refs"].items():
        _profile_receipt(registry, ref, current, observations=["installed-artifact", platform])
    if completion.current_binding(store, control) != current or current != control["request"]["binding"]:
        raise ValidationError("Release binding changed during check")
    current_artifacts(store, control)
    approval = db.execute("SELECT actor,digest,expires FROM completion_approvals WHERE id=?", (vid,)).fetchone()
    if not approval or approval[:2] != (control["approval_actor"], v.digest(control)):
        raise PermissionError("Authenticated action-scoped release approval missing")
    instant = now()
    if (not v.stamp(control["request"]["issued_at"]) <= instant < v.stamp(control["request"]["expires_at"])
            or instant >= v.stamp(approval[2])):
        raise PermissionError("Release request or approval expired")
    for record in registry.values():
        start, finish = v.stamp(record["started_at"]), v.stamp(record["finished_at"])
        if (not v.stamp(control["request"]["issued_at"]) <= start <= finish <= instant
                or (instant - finish).total_seconds() > control["request"]["max_age_seconds"]):
            raise ValidationError("Release evidence expired")
    return results["security"]["run_id"], current


def check(store, vid, request_id, expected_mission_revision, actor):
    require(store)
    v.opaque(vid)
    if not isinstance(expected_mission_revision, str) or not v.SHA.fullmatch(expected_mission_revision):
        raise ValidationError("Expected mission revision required")
    payload = {"operation": "release_check", "verification_id": vid,
               "expected_mission_revision": expected_mission_revision}
    def build(db):
        security_run, binding = validate(store, db, vid, actor)
        if binding["mission_revision"] != expected_mission_revision:
            raise ValidationError("Release mission revision differs")
        if db.execute("SELECT 1 FROM completion_runs WHERE security_run_id=?", (security_run,)).fetchone():
            raise ConflictError("Security run already reserved by completion")
        try:
            db.execute("INSERT INTO release_runs VALUES(?,?,?,?,?,?)",
                       (request_id, vid, security_run, actor, _json(payload), "prepared"))
        except sqlite3.IntegrityError as exc:
            raise ConflictError("Release verification or Security run already reserved") from exc
        body = _json({"verification_id": vid, "control_digest": v.digest(control_row(db, vid)[0]),
                      "artifact_manifest_sha256": binding["artifact_sha256"],
                      "validation_instance": True, "enforcement_active": False,
                      "release_ready": False, "released": False})
        path = f"Runs/release-validation/{_hash(vid.encode())}.json"
        if store._bytes(path) is not None:
            raise ConflictError("Release validation receipt already exists")
        return [(path, body)], {"verification_id": vid, "artifact_manifest_sha256": binding["artifact_sha256"],
                               "eligible_in_inactive_fixture": True, "validation_instance": True,
                               "enforcement_active": False, "managed_transition_performed": False,
                               "release_ready": False, "released": False}
    return store._operation(request_id, actor, payload, build, with_db=True)


def recovery_guard(store, db, request_id, intent):
    row = db.execute("SELECT verification_id,actor,payload FROM release_runs WHERE operation_id=?", (request_id,)).fetchone()
    if row is None:
        return None
    vid, actor, payload = row
    data = json.loads(payload)
    if len(intent) != 1 or intent[0]["path"] != f"Runs/release-validation/{_hash(vid.encode())}.json" or data["verification_id"] != vid:
        raise ValidationError("Release prepared intent differs")
    if _hash(store._bytes(intent[0]["path"])) == _hash(intent[0]["text"].encode()):
        return None
    def final():
        _, binding = validate(store, db, vid, actor)
        if binding["mission_revision"] != data["expected_mission_revision"]:
            raise ValidationError("Release mission changed at finalization")
    return final


def reject(db, request_id):
    db.execute("UPDATE release_runs SET state='rejected' WHERE operation_id=?", (request_id,))


def applied(db, request_id):
    db.execute("UPDATE release_runs SET state='applied' WHERE operation_id=?", (request_id,))
