"""Pure adapter from one selected conversation delta to a reviewable Memory request.

The caller selects the minimum reusable claim and excludes secrets before this
function is called. It never reads a transcript, calls a service, or writes a
Store. A digest binds a later review to the exact candidate; it is not owner
approval or an authorization to change Brain, Missions, or guidance.
"""

import hashlib
import hmac
import json
import re

from .selective_memory import _prepare_capture, parse_capture_json, schema


SKIP_REASONS = {
    "no_save": "explicit_nonretention",
    "secret": "excluded_secret",
    "transient": "transient",
}
SELECTIONS = {"decision", "correction"}
CAPTURE_FIELDS = set(schema()["capture_required"] + schema()["optional"]) - {"retain", "kind"}
EVENT_FIELDS = CAPTURE_FIELDS | {"selection", "privacy_reviewed", "reuse_reason"}
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError("Invalid conversation candidate") from None


def _validate_review(review):
    if not isinstance(review, dict) or set(review) != {"selection", "reuse_reason", "payload"}:
        raise ValueError("Invalid candidate review")
    selection = review["selection"]
    if not isinstance(selection, str) or selection not in SELECTIONS:
        raise ValueError("Invalid selected event kind")
    reason = review["reuse_reason"]
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 300:
        raise ValueError("A short reuse reason is required")
    payload = review["payload"]
    if not isinstance(payload, dict):
        raise ValueError("Invalid capture payload")
    if payload.get("retain") is not True or payload.get("kind") != "delta":
        raise ValueError("Candidate must retain one selected delta")
    if selection == "correction" and not payload.get("supersedes"):
        raise ValueError("A correction needs a superseded selective note ID")
    if selection == "decision" and payload.get("supersedes"):
        raise ValueError("Use correction for a superseding decision")
    raw = _json(payload)
    _prepare_capture(parse_capture_json(raw))
    return raw


def propose_conversation_capture(event):
    """Return a payload-free skip or a candidate for explicit caller review.

    Excluded events must be exactly ``{"selection": "no_save"}``, ``secret``, or
    ``transient``. Retained events are a selected delta, not a transcript, and
    require the caller's privacy review and concrete future-use reason.
    """
    if not isinstance(event, dict):
        raise ValueError("Conversation event must be an object")
    selection = event.get("selection")
    if not isinstance(selection, str):
        raise ValueError("Invalid selected event kind")
    if selection in SKIP_REASONS:
        if set(event) != {"selection"}:
            raise ValueError("Excluded event must be payload-free")
        return {"status": "skipped", "reason": SKIP_REASONS[selection]}
    if selection not in SELECTIONS or set(event) - EVENT_FIELDS:
        raise ValueError("Unsupported conversation event")
    if event.get("privacy_reviewed") is not True:
        raise ValueError("Privacy review is required before candidacy")

    payload = {key: event[key] for key in CAPTURE_FIELDS if key in event}
    payload.update({"retain": True, "kind": "delta"})
    review = {"selection": selection, "reuse_reason": event.get("reuse_reason"),
              "payload": payload}
    _validate_review(review)
    digest = hashlib.sha256(_json(review).encode("utf-8")).hexdigest()
    return {"status": "candidate", "review": review, "sha256": digest}


def reviewed_capture_request(candidate, reviewed_sha256):
    """Encode the exact reviewed candidate for existing ``memory_capture``.

    The caller must compare the candidate's claim, scope, source, evidence and
    exclusion decision before supplying its digest. The digest only detects
    changes after that review; it does not prove a human approved the claim.
    """
    if not isinstance(candidate, dict) or set(candidate) != {"status", "review", "sha256"}:
        raise ValueError("Invalid capture candidate")
    if candidate["status"] != "candidate":
        raise ValueError("Only a retained candidate can be submitted")
    raw = _validate_review(candidate["review"])
    digest = hashlib.sha256(_json(candidate["review"]).encode("utf-8")).hexdigest()
    stated = candidate["sha256"]
    if (not isinstance(stated, str) or not SHA256.fullmatch(stated) or
            not isinstance(reviewed_sha256, str) or not SHA256.fullmatch(reviewed_sha256) or
            not hmac.compare_digest(digest, stated) or
            not hmac.compare_digest(digest, reviewed_sha256)):
        raise ValueError("Candidate changed after review")
    return {"request_json": raw}
