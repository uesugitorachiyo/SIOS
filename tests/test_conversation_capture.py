"""Pure selected-conversation adapter tests using fabricated content only."""

import json
import unittest

from aios.conversation_capture import (
    propose_conversation_capture, reviewed_capture_request,
)


EVENT = {
    "selection": "decision", "privacy_reviewed": True,
    "reuse_reason": "Avoid repeating a synthetic workflow choice",
    "request_id": "synthetic-conversation-decision-1",
    "meaning_id": "synthetic-conversation-decision-1",
    "claim": "For this fixture, use the synthetic marker captureadapteralpha.",
    "source_type": "direct_user", "source_actor": "fixture-owner",
    "source_reference": "Memory/notes/synthetic-source.md", "project": "synthetic",
    "conditions": ["fixture only"], "outcome": "Record a reusable synthetic choice",
    "evidence_status": "stated", "recorded_at": "2026-10-20T00:00:00+00:00",
    "retrieval_cue": "captureadapteralpha",
}


class ConversationCaptureTests(unittest.TestCase):
    def test_decision_is_reviewable_before_existing_capture_call(self):
        candidate = propose_conversation_capture(EVENT)
        self.assertEqual(candidate["status"], "candidate")
        self.assertEqual(candidate["review"]["payload"]["source_reference"],
                         EVENT["source_reference"])
        self.assertEqual(candidate, propose_conversation_capture(EVENT))
        request = reviewed_capture_request(candidate, candidate["sha256"])
        payload = json.loads(request["request_json"])
        self.assertEqual(payload["claim"], EVENT["claim"])
        self.assertEqual((payload["retain"], payload["kind"]), (True, "delta"))
        self.assertNotIn("reuse_reason", payload)
        self.assertNotIn("privacy_reviewed", payload)

    def test_changed_candidate_or_wrong_review_digest_cannot_submit(self):
        candidate = propose_conversation_capture(EVENT)
        with self.assertRaisesRegex(ValueError, "changed after review"):
            reviewed_capture_request(candidate, "0" * 64)
        candidate["review"]["payload"]["claim"] = "Changed after review."
        with self.assertRaisesRegex(ValueError, "changed after review"):
            reviewed_capture_request(candidate, candidate["sha256"])

    def test_exclusions_are_payload_free(self):
        for selection in ("no_save", "secret", "transient"):
            with self.subTest(selection=selection):
                skipped = propose_conversation_capture({"selection": selection})
                self.assertEqual(skipped["status"], "skipped")
                self.assertEqual(set(skipped), {"status", "reason"})
                with self.assertRaises(ValueError):
                    reviewed_capture_request(skipped, "0" * 64)
                with self.assertRaisesRegex(ValueError, "payload-free") as captured:
                    propose_conversation_capture({"selection": selection,
                                                   "claim": "PRIVATE-SENTINEL"})
                self.assertNotIn("PRIVATE-SENTINEL", str(captured.exception))

    def test_correction_requires_link_and_distinct_identity(self):
        correction = dict(EVENT, selection="correction",
                          request_id="synthetic-conversation-correction-1",
                          meaning_id="synthetic-conversation-correction-1")
        with self.assertRaisesRegex(ValueError, "superseded"):
            propose_conversation_capture(correction)
        correction["supersedes"] = "selective-" + "a" * 64
        candidate = propose_conversation_capture(correction)
        self.assertEqual(candidate["review"]["payload"]["supersedes"],
                         correction["supersedes"])
        with self.assertRaisesRegex(ValueError, "Use correction"):
            propose_conversation_capture(dict(EVENT, supersedes=correction["supersedes"]))

    def test_requires_explicit_selection_and_valid_provenance(self):
        with self.assertRaisesRegex(ValueError, "Privacy review"):
            propose_conversation_capture(dict(EVENT, privacy_reviewed=False))
        with self.assertRaisesRegex(ValueError, "Unsupported conversation event"):
            propose_conversation_capture(dict(EVENT, transcript="unselected transcript"))
        with self.assertRaisesRegex(ValueError, "Assistant inference cannot be verified"):
            propose_conversation_capture(dict(EVENT, source_type="assistant_inference"))


if __name__ == "__main__":
    unittest.main()
