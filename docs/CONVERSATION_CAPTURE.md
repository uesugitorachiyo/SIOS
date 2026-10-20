# Selected conversation capture contract

SIOS has an authenticated `memory_capture(request_json)` tool. An authorized
Codex conversation can already select a useful delta and call it directly.
This module does not read a full conversation. A client must supply one
selected event through its authorized tool route.

`aios.conversation_capture` is a pure caller-side adapter for a client that
already has one **selected** decision or correction. It makes the selection
reviewable before using the existing tool. It does not open a connection,
grant a scope, save a note, or perform the semantic privacy review itself.
The client must choose the minimum reusable claim, establish a real source
reference, omit secrets and unrelated content, and decide whether it is worth
retaining. Passing `privacy_reviewed=True` records that caller judgment; it
is not an automatic secret scanner or owner approval.

The input is one object with `selection="decision"` or `"correction"`,
`privacy_reviewed=True`, a short `reuse_reason`, and the usual selected
`memory_capture` fields except `retain` and `kind`. Use a stable `request_id`
and `recorded_at` across retries. A correction needs a new `meaning_id` and
`request_id`, plus `supersedes` set to the old selective note's path stem.
Provenance remains explicit: `source_type`, attributed `source_actor`,
`source_reference`, evidence status and applicable project/conditions.
`source_actor` is not the authenticated device identity.

For excluded material, call the adapter with only
`{"selection":"no_save"}`, `{"selection":"secret"}`, or
`{"selection":"transient"}`. It returns a payload-free skip and no capture
request. Supplying any content alongside these choices is rejected without
echoing that content. Do not transmit excluded content to the server or put it
in diagnostics or a Brain proposal.

```python
from aios.conversation_capture import (
    propose_conversation_capture, reviewed_capture_request,
)

event = {
    "selection": "decision", "privacy_reviewed": True,
    "reuse_reason": "Avoid repeating this synthetic workflow choice",
    "request_id": "fixture-choice-1", "meaning_id": "fixture-choice-1",
    "claim": "For this fixture, use marker captureadapteralpha.",
    "source_type": "direct_user", "source_actor": "fixture-owner",
    "source_reference": "Memory/notes/synthetic-source.md",
    "project": "synthetic", "conditions": ["fixture only"],
    "outcome": "Record a reusable synthetic choice", "evidence_status": "stated",
    "recorded_at": "2026-01-01T00:00:00+00:00",
    "retrieval_cue": "captureadapteralpha",
}
candidate = propose_conversation_capture(event)
# Review candidate["review"]: claim, source, scope, evidence, reuse reason.
params = reviewed_capture_request(candidate, candidate["sha256"])
# An already authorized client submits params["request_json"] through its
# existing memory_capture tool. This adapter never performs that call.
```

The digest detects edits after review; copying the digest without inspecting
the candidate does not constitute review. `reviewed_capture_request` only
formats exact bytes for `memory_capture`; the central service still enforces
`read` plus `memory:write`, validation, request-id conflict handling,
deduplication and durable receipts.

After a `captured` receipt, use its path stem and revision with
`memory_index_refresh` and a unique plain-text marker. An index timeout does
not roll back the capture: read the committed path before any retry. A later
session searches the index, then reads the full current source and checks
revision, provenance, conditions and supersession before applying it. Search
snippets are navigation, not current authority. A duplicate receipt creates
no second substantive note. A correction preserves the prior note as history.
Neither route changes canonical Brain or current Mission state.

## Synthetic close-session preparation

`aios.close_session_dry_run.prepare_close_session` accepts only an explicit
`{"fixture_only": true, "events": [...]}` object. Each event is already
selected by the caller; this is not a transcript parser. `decision` and
`correction` use the selected-event fields above. A `next_action` needs an
explicit action, source reference, `Missions/<slug>/mission.md` path, optional
64-character current revision, `basis` (`stated` or `observed`), a supplied
`mission_authority` (`sios`, `aios`, or `unknown`), and
`source_actor`, timezone-aware `recorded_at`, and `privacy_reviewed=true`.
It stays `review_only` and always needs a fresh
authoritative Mission read before anyone considers an update.

Excluded events are payload-free `no_save`, `secret`, or `transient` markers.
The dry-run blocks common obvious credential strings and never echoes a
blocked payload, but it is not a complete privacy scanner; the caller still
must omit excluded material before passing an event. Exact same-request
retries are skipped; changed content under a request ID is a conflict;
the same semantic note identity under another request ID requires review.
Corrections retain the old note as history through `supersedes`.

Output is a review checklist and existing `conversation_capture` candidate,
not a submission. Real session collection, current-Mission validation and
Brain promotion are outside this dry-run. Real conversation capture requires
separate authorization and end-to-end validation.

Focused synthetic tests cover candidate review, exclusion and changed-content
rejection; an isolated temporary service/index test covers decision and
correction capture, duplicate/nonretention, Basic Memory indexing, and a fresh
client's full read. This does not qualify real conversation ingestion.
Activating this adapter in a particular client requires a
supported way for that client to supply one selected event and review it.
