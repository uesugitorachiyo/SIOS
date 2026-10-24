"""Versioned QA/Security intake for the INACTIVE, same-user advisory pilot.

No mission, approval, release, network or privileged reference writes. The caller
must hold the control-side request and receipt registry independently of workers.
Neither a JSON flag nor this CLI authenticates that caller. M4/M5 consumers are
deliberately not connected here.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import unicodedata

VERSION = "1.0"
SEC = tuple(f"SEC-{i:02}" for i in range(1, 11))
DIMENSIONS = {"reach", "security_exposure", "domain_consequence", "reversibility", "uncertainty"}
BINDING_KEYS = {
    "project_id", "mission_id", "mission_revision", "stage", "snapshot_id",
    "content_sha256", "scope_sha256", "spec_sha256", "acceptance_sha256",
    "policy_sha256", "profile_sha256", "rules_sha256", "runner_sha256",
    "permissions_sha256", "corpus_sha256", "adapter_sha256", "model", "platform",
    "artifact_sha256",
}
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")


def canonical(value):
    def check(v):
        if v is None or type(v) is bool:
            return
        if type(v) is int and abs(v) <= 9007199254740991:
            return
        if isinstance(v, str):
            if unicodedata.normalize("NFC", v) != v:
                raise ValueError("non-NFC string")
            v.encode("utf-8")
            return
        if isinstance(v, list):
            for item in v:
                check(item)
            return
        if isinstance(v, dict):
            for key, item in v.items():
                if not isinstance(key, str):
                    raise ValueError("non-string key")
                check(key)
                check(item)
            return
        raise ValueError("non-portable JSON value")
    check(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def parse(text):
    def unique(pairs):
        out = {}
        for k, v in pairs:
            if k in out:
                raise ValueError("duplicate JSON key")
            out[k] = v
        return out
    value = json.loads(text, object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    canonical(value)
    return value


def exact(obj, keys):
    if not isinstance(obj, dict) or set(obj) != set(keys):
        raise ValueError("missing or unknown fields")


def opaque(ref):
    if not isinstance(ref, str) or not REF.fullmatch(ref):
        raise ValueError("reference is not an opaque registry ID")
    return ref


def stamp(value):
    if not isinstance(value, str) or not value.endswith(("Z", "+00:00")):
        raise ValueError("UTC timestamp required")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def validate_security_schema(security):
    """Reject malformed report timestamps even if an optional format plugin is absent."""
    from jsonschema import Draft202012Validator, FormatChecker
    schema = parse((Path(__file__).with_name('verification_data') / 'security-report.schema.json').read_text(encoding='utf-8'))
    format_checker = FormatChecker()
    if 'date-time' not in format_checker.checkers:
        raise ValueError('Security date-time validation unavailable')
    Draft202012Validator(schema, format_checker=format_checker).validate(security)
    advisory_at = security['environment']['advisory_snapshot_at']
    if advisory_at is not None:
        stamp(advisory_at)


def path_name(value):
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or any(part in ("", ".", "..") for part in value.split("/"))
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or unicodedata.normalize("NFC", value) != value):
        raise ValueError("unsafe portable relative path")
    return value


def capture(root, names, project_id, snapshot_id):
    """Read a caller-resolved regular-file scope, refusing links and changed files.

    Scope selection is NOT inferred from file extensions or approved by this call.
    The coordinator inventories ignored/generated/index inputs separately. A
    concurrent hostile directory replacement remains outside this advisory lane.
    """
    root = Path(root).absolute()
    if root.is_symlink() or root.resolve() != root:
        raise ValueError("root alias")
    seen, entries = set(), []
    for name in sorted(names):
        path_name(name)
        if name.casefold() in seen:
            raise ValueError("duplicate or case-colliding input")
        seen.add(name.casefold())
        path = root / name
        for parent in [path, *path.parents]:
            if parent == root:
                break
            if parent.is_symlink():
                raise ValueError("symlink input")
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("non-regular input")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError("input changed during open")
            data = stream.read()
            after = os.fstat(stream.fileno())
        final = path.lstat()
        if any((s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode) !=
               (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_mode)
               for s in (opened, after, final)):
            raise ValueError("input changed during capture")
        entries.append({"root_id": "working", "path": name, "size_bytes": len(data),
                        "content_sha256": hashlib.sha256(data).hexdigest(),
                        "executable": bool(before.st_mode & 0o111) if os.name != "nt" else False})
    if not entries:
        raise ValueError("empty scope")
    return {"schema_version": "1.1", "kind": "content", "project_id": opaque(project_id),
            "snapshot_id": opaque(snapshot_id), "entries": entries}


def classify(assessment, control, requirements):
    """Only control-side inspected facts are accepted; UNKNOWN never defaults low."""
    exact(assessment, {"dimensions", "protected_triggers", "cross_module", "qa_structural_refactor"})
    exact(control, {"allow_fast", "allow_reviewer_omission", "require_independent"})
    if any(type(v) is not bool for v in control.values()):
        raise ValueError("control booleans required")
    exact(assessment["dimensions"], DIMENSIONS)
    for record in assessment["dimensions"].values():
        exact(record, {"known", "low", "evidence_refs"})
        if type(record["known"]) is not bool or type(record["low"]) is not bool:
            raise ValueError("dimension booleans required")
        if not isinstance(record["evidence_refs"], list):
            raise ValueError("dimension evidence required")
        for ref in record["evidence_refs"]:
            opaque(ref)
    triggers = assessment["protected_triggers"]
    if not isinstance(triggers, list) or any(not isinstance(t, str) or not t for t in triggers):
        raise ValueError("invalid trigger list")
    if any(type(assessment[k]) is not bool for k in ("cross_module", "qa_structural_refactor")):
        raise ValueError("impact booleans required")
    dimensions = assessment["dimensions"].values()
    if any(not d["known"] or not d["evidence_refs"] for d in dimensions):
        return {"route": "UNKNOWN", "reviewer_dispatch": "REQUIRED", "reason": "unresolved impact"}
    independent = control["require_independent"] or any(r["evidence_kind"] == "independent_review" for r in requirements)
    if triggers:
        route = "PROTECTED"
    elif assessment["cross_module"] or assessment["qa_structural_refactor"] or independent:
        route = "STANDARD"
    elif control["allow_fast"] and all(d["low"] for d in dimensions):
        route = "FAST"
    else:
        route = "STANDARD"
    omit = route == "FAST" and control["allow_reviewer_omission"] and not independent
    return {"route": route, "reviewer_dispatch": "NOT_REQUIRED_BY_POLICY" if omit else "REQUIRED",
            "reason": "control-side dimensions and requirement evidence paths"}


def binding_valid(binding):
    exact(binding, BINDING_KEYS)
    for key in BINDING_KEYS:
        value = binding[key]
        if key.endswith("sha256"):
            if value is None and key in {"artifact_sha256", "corpus_sha256"}:
                continue
            if not isinstance(value, str) or not SHA.fullmatch(value):
                raise ValueError("missing digest binding")
        elif not isinstance(value, str) or not value:
            raise ValueError("missing identity binding")
    if binding["stage"] not in {"preflight", "change", "complete", "release"}:
        raise ValueError("unknown stage")
    if binding["platform"] not in {"macos", "windows", "linux"}:
        raise ValueError("unknown platform")
    for key in ("project_id", "mission_id", "snapshot_id"):
        opaque(binding[key])
    if not SHA.fullmatch(binding["mission_revision"]):
        raise ValueError("mission revision digest required")


def validate_request(request):
    exact(request, {"schema_version", "request_id", "binding", "developer_session", "reviewer_session",
                    "reviewer_actor", "assessment", "route_control", "requirements", "security_dispositions",
                    "issued_at", "expires_at", "max_age_seconds", "role_documents", "perspectives", "selected_adapters"})
    if request["schema_version"] != VERSION:
        raise ValueError("unsupported request version")
    opaque(request["request_id"])
    binding_valid(request["binding"])
    for key in ("developer_session", "reviewer_session", "reviewer_actor"):
        opaque(request[key])
    if type(request["max_age_seconds"]) is not int or request["max_age_seconds"] <= 0:
        raise ValueError("invalid freshness limit")
    if stamp(request["expires_at"]) <= stamp(request["issued_at"]):
        raise ValueError("invalid validity interval")
    reqs = request["requirements"]
    if not isinstance(reqs, list) or not reqs:
        raise ValueError("empty required QA coverage")
    seen = set()
    for r in reqs:
        exact(r, {"id", "topic", "evidence_kind", "min_count", "observations"})
        opaque(r["id"])
        if r["id"] in seen or r["topic"] not in {f"QA-{i:02}" for i in range(1, 7)}:
            raise ValueError("duplicate requirement or unknown topic")
        seen.add(r["id"])
        if r["evidence_kind"] not in {"mechanical", "independent_review"}:
            raise ValueError("unqualified evidence path")
        if type(r["min_count"]) is not int or r["min_count"] < 1:
            raise ValueError("nonzero evidence count required")
        if not isinstance(r["observations"], list) or not r["observations"]:
            raise ValueError("observation requirements missing")
        for obs in r["observations"]:
            opaque(obs)
    exact(request["security_dispositions"], SEC)
    for sec, disposition in request["security_dispositions"].items():
        exact(disposition, {"state", "reason", "evidence_refs"})
        if disposition["state"] not in {"selected", "not_applicable"} or not disposition["reason"]:
            raise ValueError("unresolved Security applicability")
        if sec in {"SEC-01", "SEC-02"} and disposition["state"] != "selected":
            raise ValueError("mandatory Security floor")
        if not isinstance(disposition["evidence_refs"], list) or not disposition["evidence_refs"]:
            raise ValueError("applicability evidence missing")
        for ref in disposition["evidence_refs"]:
            opaque(ref)
    selected = {sec for sec, d in request["security_dispositions"].items() if d["state"] == "selected"}
    exact(request["selected_adapters"], selected)
    for adapter in request["selected_adapters"].values():
        exact(adapter, {"name", "version", "integrity_sha256", "actor", "session"})
        for key in ("name", "version"):
            if not isinstance(adapter[key], str) or not adapter[key]:
                raise ValueError("adapter name/version missing")
        for key in ("actor", "session"):
            opaque(adapter[key])
        if not isinstance(adapter["integrity_sha256"], str) or not SHA.fullmatch(adapter["integrity_sha256"]):
            raise ValueError("adapter integrity receipt digest required")
    if not isinstance(request["role_documents"], dict) or not request["role_documents"]:
        raise ValueError("explicit role material missing")
    for ref, sha in request["role_documents"].items():
        opaque(ref)
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise ValueError("role digest missing")
    if (not isinstance(request["perspectives"], list)
            or len(set(request["perspectives"])) != len(request["perspectives"])
            or set(request["perspectives"]) - {"code_review", "architecture_review", "refactor_assessment"}):
        raise ValueError("invalid review perspectives")
    route = classify(request["assessment"], request["route_control"], reqs)
    if request["perspectives"] and route["route"] == "FAST":
        route = {"route": "STANDARD", "reviewer_dispatch": "REQUIRED", "reason": "selected independent review perspective"}
    if route["reviewer_dispatch"] == "REQUIRED" and not request["perspectives"]:
        raise ValueError("required independent review perspective missing")
    return route


def assess(request, qa, security, registry, current_binding, *, now=None, consumed=()):
    """Advisory result: no auth implied and no actual transition performed.

    Registry entries hold bytes collected independently by the supervisor. A
    future authenticated consumer must supply these inputs and recheck them under
    its existing CAS/lock. Self-declared result authority is never consulted.
    """
    now = now or datetime.now(timezone.utc)
    blockers = []
    route = {"route": "UNKNOWN", "reviewer_dispatch": "REQUIRED"}
    try:
        route = validate_request(request)
        for adapter in request["selected_adapters"].values():
            if (adapter["actor"] == request["reviewer_actor"]
                    or adapter["session"] in {request["reviewer_session"], request["developer_session"]}):
                raise ValueError("Security producer is not independent")
        binding_valid(current_binding)
        if request["binding"] != current_binding:
            blockers.append("current candidate/control/mission binding changed")
        if request["request_id"] in consumed:
            blockers.append("replayed request")
        if not stamp(request["issued_at"]) <= now < stamp(request["expires_at"]):
            blockers.append("request expired or future dated")
        if route["route"] == "UNKNOWN":
            blockers.append("unresolved risk classification")
        if request["binding"]["stage"] == "release":
            blockers.append("release consumer/approvals/artifact qualification is outside advisory pilot")

        def receipt(ref, *, actor=None, kind=None, session=None, count=1, observations=()):
            opaque(ref)
            record = registry.get(ref)
            exact(record, {"bytes", "sha256", "binding", "actor", "session", "kind", "count",
                           "observations", "started_at", "finished_at"})
            if not isinstance(record["bytes"], bytes) or not record["bytes"]:
                raise ValueError("empty evidence")
            if hashlib.sha256(record["bytes"]).hexdigest() != record["sha256"]:
                raise ValueError("altered evidence")
            if record["binding"] != request["binding"]:
                raise ValueError("evidence target/config mismatch")
            start, finish = stamp(record["started_at"]), stamp(record["finished_at"])
            if not stamp(request["issued_at"]) <= start <= finish <= now:
                raise ValueError("invalid evidence time")
            if (now - finish).total_seconds() > request["max_age_seconds"]:
                raise ValueError("stale evidence")
            if actor and record["actor"] != actor or kind and record["kind"] != kind:
                raise ValueError("wrong evidence producer/kind")
            if type(record["count"]) is not int or record["count"] < count:
                raise ValueError("insufficient execution count")
            if not isinstance(record["observations"], list) or not set(observations) <= set(record["observations"]):
                raise ValueError("missing observations")
            if session is not None and record["session"] != session:
                raise ValueError("review session mismatch")
            return record

        # Verify applicability, inspected dimensions and role material from caller registry.
        for d in request["security_dispositions"].values():
            for ref in d["evidence_refs"]:
                receipt(ref)
        for d in request["assessment"]["dimensions"].values():
            for ref in d["evidence_refs"]:
                receipt(ref)
        for ref, sha in request["role_documents"].items():
            if receipt(ref)["sha256"] != sha:
                raise ValueError("role material changed")

        exact(qa, {"schema_version", "request_id", "binding", "actor", "session", "route",
                   "reviewer_dispatch", "requirements", "findings", "consumed_documents", "perspectives"})
        if qa["schema_version"] != VERSION or qa["request_id"] != request["request_id"]:
            raise ValueError("wrong QA version/request")
        if qa["binding"] != request["binding"] or qa["route"] != route["route"] or qa["reviewer_dispatch"] != route["reviewer_dispatch"]:
            raise ValueError("QA target or route mismatch")
        if qa["consumed_documents"] != request["role_documents"]:
            raise ValueError("review context/spec material missing")
        if route["reviewer_dispatch"] == "REQUIRED":
            if qa["actor"] != request["reviewer_actor"] or qa["session"] != request["reviewer_session"] or qa["session"] == request["developer_session"]:
                raise ValueError("modifier cannot supply independent review")
        if not isinstance(qa["requirements"], list):
            raise ValueError("QA coverage missing")
        rows = {}
        for row in qa["requirements"]:
            exact(row, {"id", "outcome", "evidence_refs", "reason"})
            opaque(row["id"])
            if row["id"] in rows:
                raise ValueError("duplicate QA requirement")
            rows[row["id"]] = row
        if set(rows) != {r["id"] for r in request["requirements"]}:
            raise ValueError("required QA IDs missing or unexpected")
        for r in request["requirements"]:
            row = rows[r["id"]]
            if row["outcome"] != "PASS":
                blockers.append("unverified QA requirement: " + r["id"])
                continue
            if not isinstance(row["evidence_refs"], list) or not row["evidence_refs"]:
                raise ValueError("unsupported QA PASS")
            for ref in row["evidence_refs"]:
                receipt(ref, actor=request["reviewer_actor"] if r["evidence_kind"] == "independent_review" else None,
                        kind=r["evidence_kind"],
                        session=request["reviewer_session"] if r["evidence_kind"] == "independent_review" else None,
                        count=r["min_count"], observations=r["observations"])
        if not isinstance(qa["perspectives"], list):
            raise ValueError("review perspective outcomes missing")
        perspectives = {}
        for row in qa["perspectives"]:
            exact(row, {"id", "outcome", "evidence_refs", "reason"})
            if row["id"] in perspectives:
                raise ValueError("duplicate review perspective")
            perspectives[row["id"]] = row
        if set(perspectives) != set(request["perspectives"]):
            raise ValueError("requested review perspective coverage missing")
        for perspective, row in perspectives.items():
            if row["outcome"] != "PASS":
                blockers.append("unverified review perspective: " + perspective)
            elif not isinstance(row["evidence_refs"], list) or not row["evidence_refs"]:
                raise ValueError("unsupported review perspective PASS")
            else:
                for ref in row["evidence_refs"]:
                    receipt(ref, actor=request["reviewer_actor"], kind="independent_review",
                            session=request["reviewer_session"],
                            observations=[perspective, "context-consumed"])
        seen = set()
        if not isinstance(qa["findings"], list):
            raise ValueError("QA findings missing")
        for finding in qa["findings"]:
            exact(finding, {"id", "requirement_id", "blocking", "status", "evidence_refs", "summary"})
            opaque(finding["id"])
            if finding["id"] in seen or finding["requirement_id"] not in rows or type(finding["blocking"]) is not bool:
                raise ValueError("invalid QA finding identity")
            seen.add(finding["id"])
            if finding["status"] not in {"OPEN", "FIXED", "DEFERRED"}:
                raise ValueError("invalid QA disposition")
            if finding["blocking"] and finding["status"] != "FIXED":
                blockers.append("blocking QA finding: " + finding["id"])
            if not finding["evidence_refs"]:
                raise ValueError("finding evidence missing")
            for ref in finding["evidence_refs"]:
                if finding["blocking"] and finding["status"] == "FIXED":
                    receipt(ref, actor=request["reviewer_actor"], kind="independent_review",
                            session=request["reviewer_session"],
                            observations=["fix-verified", finding["id"]])
                else:
                    receipt(ref)

        validate_security_schema(security)
        if security["record_kind"] != "execution":
            raise ValueError("Security template is not execution")
        b = request["binding"]
        if any(security[k] != b[k] for k in ("project_id", "mission_id", "stage")):
            raise ValueError("Security project/mission/stage mismatch")
        if security["candidate"] != {"commit": security["candidate"]["commit"], "content_sha256": b["content_sha256"],
                                      "scope_manifest_sha256": b["scope_sha256"], "artifact_manifest_sha256": b["artifact_sha256"]}:
            raise ValueError("Security candidate mismatch")
        if any(security["control"][k] != b[k] for k in ("policy_sha256", "profile_sha256", "rules_sha256", "runner_sha256")):
            raise ValueError("Security controls mismatch")
        if security["environment"]["platform"] != b["platform"]:
            raise ValueError("Security platform mismatch")
        if not stamp(request["issued_at"]) <= stamp(security["started_at"]) <= stamp(security["finished_at"]) <= now:
            raise ValueError("Security time mismatch")
        opaque(security["run_id"])
        if security["run_id"] in consumed:
            raise ValueError("replayed Security run")
        checks = {c["id"]: c for c in security["checks"]}
        if len(checks) != len(security["checks"]) or set(checks) != set(SEC):
            raise ValueError("Security category coverage missing/duplicate")
        finding_ids = {f["id"] for f in security["findings"]}
        if len(finding_ids) != len(security["findings"]):
            raise ValueError("duplicate Security finding")
        if security["decision"] != "PASS" or security["exception_refs"]:
            blockers.append("Security block or exception requires qualified owner path")
        for sec, c in checks.items():
            selected = request["security_dispositions"][sec]["state"] == "selected"
            adapter = request["selected_adapters"].get(sec)
            if selected:
                # SEC-02 requires the selected secret scanner, not a review substitute.
                if sec == "SEC-02" and c["kind"] != "automated":
                    raise ValueError("SEC-02 requires automated secret scanning")
                if c["tool"]["name"] != adapter["name"] or c["tool"]["version"] != adapter["version"]:
                    raise ValueError("unapproved Security adapter identity")
                if receipt(c["tool"]["integrity_ref"])["sha256"] != adapter["integrity_sha256"]:
                    raise ValueError("unapproved Security adapter integrity")
            if c["required"] != selected or c["outcome"] != ("PASS" if selected else "NOT_APPLICABLE"):
                blockers.append("unverified Security category: " + sec)
            if not c["evidence_refs"]:
                raise ValueError("Security evidence missing")
            if selected and (not c["coverage"]["complete"] or c["coverage"]["omissions"]):
                blockers.append("incomplete Security observations: " + sec)
            if selected and c["kind"] == "automated" and c["exit_code"] != 0:
                blockers.append("nonzero scanner exit: " + sec)
            if selected:
                if not stamp(security["started_at"]) <= stamp(c["started_at"]) <= stamp(c["finished_at"]) <= stamp(security["finished_at"]):
                    raise ValueError("Security check time outside run")
                if c["kind"] == "automated":
                    counts = c["coverage"]
                    if (type(counts["expected_file_count"]) is not int or counts["expected_file_count"] <= 0
                            or counts["expected_file_count"] != counts["scanned_file_count"]):
                        raise ValueError("Security count coverage incomplete")
            if not set(c["finding_refs"]) <= finding_ids:
                raise ValueError("unresolved Security finding reference")
            for ref in c["evidence_refs"]:
                expected_kind = ("automated" if c["kind"] == "automated" else "independent_review") if selected else None
                record = receipt(ref, kind=expected_kind, actor=adapter["actor"] if selected else None,
                                 session=adapter["session"] if selected else None)
                if selected and record["session"] != adapter["session"]:
                    raise ValueError("Security producer session mismatch")
                if selected and not {"executed", "observed"} <= set(record["observations"]):
                    raise ValueError("Security execution not observed")
        for f in security["findings"]:
            # No exception or FIXED flag authenticates a dismissal. Require a later
            # qualified run, not normalization of old raw findings in this pilot.
            blockers.append("raw Security finding requires qualified resolution: " + f["id"])
            for ref in f["evidence_refs"]:
                receipt(ref)
    except (ValueError, TypeError, KeyError, OSError, AttributeError) as e:
        blockers.append("invalid intake: " + str(e))
    except Exception as e:
        # Schema/library errors remain incomplete; never default to eligible.
        blockers.append("validation error: " + type(e).__name__)
    return {"schema_version": VERSION, "record_kind": "advisory_assessment", **route,
            "advisory_eligible": not blockers, "blockers": list(dict.fromkeys(blockers)),
            "enforcement_active": False, "technical_boundary": "same_user_advisory",
            "managed_transition_performed": False, "lifecycle": "Implemented",
            "verification_claim": False, "release_ready": False, "released": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packet", type=Path, help="local supervisor packet; not an authenticated gate")
    args = parser.parse_args()
    packet = parse(args.packet.read_text(encoding="utf-8"))
    exact(packet, {"request", "qa", "security", "registry", "current_binding", "consumed"})
    for record in packet["registry"].values():
        # No privileged dereferencing: evidence is supplied inline by the caller.
        record["bytes"] = record["bytes"].encode("utf-8")
    result = assess(**packet)
    print(json.dumps(result, indent=2))
    return 0 if result["advisory_eligible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
