"""Synthetic-only close-session checkpoint tests; no client or Store."""

import unittest

from aios.close_session_dry_run import prepare_close_session
from tests.test_conversation_capture import EVENT


def run(*events):
    return prepare_close_session({"fixture_only": True, "events": list(events)})["items"]


class CloseSessionDryRunTests(unittest.TestCase):
    def test_decision_retry_and_semantic_dedup(self):
        second_id = dict(EVENT, request_id="synthetic-conversation-decision-2")
        items = run(EVENT, EVENT, second_id)
        self.assertEqual([item["status"] for item in items],
                         ["candidate_for_review", "skipped", "blocked"])
        self.assertEqual(items[1]["reason"], "duplicate_retry")
        self.assertEqual(items[2]["reason"], "semantic_identity_requires_review")
        self.assertEqual(items[0]["authority"], "historical_memory_evidence_only")

    def test_same_request_id_with_changed_claim_is_conflict(self):
        items = run(EVENT, dict(EVENT, claim="A different synthetic decision."))
        self.assertEqual(items[1]["reason"], "request_id_conflict")

    def test_correction_requires_and_retains_supersession(self):
        first = run(EVENT)[0]
        correction = dict(EVENT, selection="correction",
                          request_id="synthetic-correction-1", meaning_id="synthetic-correction-1",
                          claim="The earlier synthetic choice is corrected.",
                          supersedes=first["semantic_note_id"])
        self.assertEqual(run(correction)[0]["candidate"]["review"]["payload"]["supersedes"],
                         first["semantic_note_id"])
        correction.pop("supersedes")
        self.assertEqual(run(correction)[0]["reason"], "capture_evidence_incomplete")

    def test_no_save_and_obvious_secret_are_excluded_without_echo(self):
        items = run({"selection": "no_save"},
                    {"selection": "no_save", "claim": "PRIVATE-FIXTURE"},
                    dict(EVENT, claim="password: private-fixture-value"))
        self.assertEqual(items[0]["reason"], "explicit_nonretention")
        self.assertEqual(items[1]["reason"], "excluded_event_must_be_payload_free")
        self.assertEqual(items[2]["reason"], "possible_secret_excluded")
        self.assertNotIn("PRIVATE-FIXTURE", str(items))
        self.assertNotIn("private-fixture-value", str(items))

    def test_absent_evidence_and_unselected_input_are_blocked(self):
        items = run(dict(EVENT, source_reference=""),
                    dict(EVENT, selection="inferred"))
        self.assertEqual([x["status"] for x in items], ["blocked", "blocked"])
        self.assertEqual(items[0]["reason"], "capture_evidence_incomplete")
        with self.assertRaisesRegex(ValueError, "synthetic fixture"):
            prepare_close_session({"fixture_only": False, "events": [EVENT]})

    def test_next_action_is_review_only_even_with_current_label(self):
        base = {"selection": "next_action", "action": "Review fixture result",
                "source_reference": "Memory/notes/synthetic-source.md",
                "source_actor": "fixture-owner", "recorded_at": "2026-10-20T00:00:00+00:00",
                "mission_path": "Missions/synthetic/mission.md", "mission_revision": "a" * 64,
                "basis": "stated", "mission_authority": "sios", "privacy_reviewed": True}
        historical = dict(base, mission_authority="aios", mission_revision=None)
        items = run(base, historical, dict(base, basis="inferred"))
        self.assertEqual([x["status"] for x in items],
                         ["review_only", "review_only", "blocked"])
        self.assertTrue(all(x["requires_current_authoritative_read"] for x in items[:2]))
        self.assertEqual(items[1]["authority_label"], "aios")
        self.assertEqual(run(dict(base, source_reference="../outside"))[0]["status"], "blocked")
        self.assertEqual(run(dict(base, recorded_at="2026-10-20"))[0]["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
