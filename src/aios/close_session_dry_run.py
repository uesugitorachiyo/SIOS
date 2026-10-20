"""Dry-run close-session triage of explicitly selected synthetic events.

No transcript access, Store, client, or write operation is available here.
"""

from __future__ import annotations

import json
import re

from .conversation_capture import propose_conversation_capture
from .selective_memory import _date, _prepare_capture, _reference, _string


_SECRET = re.compile(
    r"(?i)(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\b(?:api[_ -]?key|password|secret|bearer)\s*[:=]\s*\S+|"
    r"\bsk-[A-Za-z0-9_-]{12,})"
)
_MISSION = re.compile(r"Missions/[a-z0-9_-]+/mission\.md\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_SKIP = {"no_save", "secret", "transient"}


def _contains_secret(event: dict) -> bool:
    # This catches common obvious credentials; it is not a privacy classifier.
    try:
        encoded = json.dumps(event, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError, RecursionError):
        return True
    return len(encoded.encode("utf-8")) > 65_536 or bool(_SECRET.search(encoded))


def _next_action(event: dict) -> dict:
    expected = {"selection", "action", "source_reference", "mission_path",
                "mission_revision", "basis", "mission_authority", "privacy_reviewed",
                "source_actor", "recorded_at"}
    if set(event) != expected or event.get("privacy_reviewed") is not True:
        raise ValueError("Next action requires exact reviewed fields")
    if not all(isinstance(event.get(key), str) and event[key].strip()
               for key in ("action", "source_reference", "mission_path", "basis", "mission_authority")):
        raise ValueError("Next action lacks a source or action")
    if len(event["action"]) > 500 or "\x00" in event["action"]:
        raise ValueError("Next action must be short text")
    _reference(event["source_reference"], "source_reference")
    source_actor = _string(event.get("source_actor"), "source_actor", max_length=120)
    recorded_at = _date(event.get("recorded_at"), "recorded_at")
    if recorded_at is None:
        raise ValueError("Next action needs a recorded_at with timezone")
    if not _MISSION.fullmatch(event["mission_path"]):
        raise ValueError("Next action needs a bounded Mission body path")
    if event["basis"] not in {"stated", "observed"}:
        raise ValueError("Next action must be explicit evidence, not inference")
    if event["mission_authority"] not in {"sios", "aios", "unknown"}:
        raise ValueError("Unknown Mission authority label")
    revision = event["mission_revision"]
    if revision is not None and (not isinstance(revision, str) or not _SHA.fullmatch(revision)):
        raise ValueError("Invalid Mission revision")
    return {
        "status": "review_only",
        "kind": "next_action",
        "action": event["action"],
        "source_reference": event["source_reference"],
        "source_actor": source_actor,
        "recorded_at": recorded_at,
        "mission_path": event["mission_path"],
        "mission_revision": revision,
        "basis": event["basis"],
        "authority_label": event["mission_authority"],
        "requires_current_authoritative_read": True,
        "reason": "Historical or caller-supplied state does not prove a current Mission action.",
    }


def prepare_close_session(session: dict) -> dict:
    """Prepare a review list from fixture-only selected events; never submit it."""
    if not isinstance(session, dict) or set(session) != {"fixture_only", "events"} or session["fixture_only"] is not True:
        raise ValueError("Only explicit synthetic fixture input is accepted")
    events = session["events"]
    if not isinstance(events, list) or len(events) > 25:
        raise ValueError("Expected at most 25 selected fixture events")
    results = []
    by_request = {}
    by_identity = {}
    for index, event in enumerate(events):
        if not isinstance(event, dict) or not isinstance(event.get("selection"), str):
            results.append({"item": index, "status": "blocked", "reason": "invalid_selected_event"})
            continue
        selection = event["selection"]
        if selection in _SKIP:
            if set(event) != {"selection"}:
                results.append({"item": index, "status": "blocked", "reason": "excluded_event_must_be_payload_free"})
            else:
                results.append({"item": index, **propose_conversation_capture(event)})
            continue
        if _contains_secret(event):
            results.append({"item": index, "status": "blocked", "reason": "possible_secret_excluded"})
            continue
        if selection == "next_action":
            try:
                results.append({"item": index, **_next_action(event)})
            except ValueError:
                results.append({"item": index, "status": "blocked", "reason": "next_action_evidence_incomplete"})
            continue
        if selection not in {"decision", "correction"}:
            results.append({"item": index, "status": "blocked", "reason": "unsupported_or_inferred_event"})
            continue
        try:
            candidate = propose_conversation_capture(event)
            payload = candidate["review"]["payload"]
            _, _, _, identity = _prepare_capture(payload)
        except ValueError:
            results.append({"item": index, "status": "blocked", "reason": "capture_evidence_incomplete"})
            continue
        request_id = payload["request_id"]
        digest = candidate["sha256"]
        if request_id in by_request:
            reason = "duplicate_retry" if by_request[request_id] == digest else "request_id_conflict"
            results.append({"item": index, "status": "skipped" if reason == "duplicate_retry" else "blocked",
                            "reason": reason})
            continue
        if identity in by_identity:
            results.append({"item": index, "status": "blocked", "reason": "semantic_identity_requires_review"})
            continue
        by_request[request_id] = digest
        by_identity[identity] = digest
        results.append({"item": index, "status": "candidate_for_review", "kind": selection,
                        "candidate": candidate, "semantic_note_id": identity,
                        "authority": "historical_memory_evidence_only"})
    return {"mode": "synthetic_dry_run", "items": results,
            "limits": "No conversation extraction, secret guarantee, current Mission attestation, Memory write, or Brain promotion."}
