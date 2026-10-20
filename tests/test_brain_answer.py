"""Isolated answer-from-brain evidence tests; no service or writer."""

import hashlib
import unittest
from datetime import date

from aios.brain_answer import assess


TODAY = date(2026, 10, 20)


def read(path, text):
    return {"path": path, "text": text,
            "revision": hashlib.sha256(text.encode()).hexdigest()}


class BrainAnswerTests(unittest.TestCase):
    def setUp(self):
        self.root = read("Brain/brain/index.md", "# Brain\n[Goals](goals/)\n")
        self.category = read("Brain/brain/goals/index.md", "[Goal](/goals/goal.md)\n[Other](/goals/other.md)\n")
        self.path = "Brain/brain/goals/goal.md"
        self.other = "Brain/brain/goals/other.md"
        self.body = ("---\nstatus: stable\nstale_after: 2026-11-01\n"
                     "sensitivity: internal\nbasis: stated\nsources:\n  - resource: synthetic\n"
                     "---\n# Goal\nThe approved direction is A.\n")
        self.concept = read(self.path, self.body)
        self.selection = {"path": self.path, "quote": "The approved direction is A.",
                          "question_key": "direction", "interpretation": "A"}

    def evaluate(self, selections=None, concepts=None, root=None):
        return assess(self.root if root is None else root,
                      {self.category["path"]: self.category},
                      {self.path: self.concept} if concepts is None else concepts,
                      [self.selection] if selections is None else selections, TODAY)

    def test_correct_full_evidence_is_ready_but_snapshot_only(self):
        result = self.evaluate()
        self.assertEqual(result["evidence_status"], "EVIDENCE_READY")
        self.assertEqual(result["brain_authority"], "SIOS_SNAPSHOT_ONLY")
        self.assertEqual(result["sources"][0]["revision"], self.concept["revision"])
        self.assertNotIn("The approved direction", str(result))

    def test_missing_index_or_full_evidence_requires_review(self):
        self.assertIn("root_index_missing_or_revision_mismatch", self.evaluate(root={})["issues"])
        self.assertIn("selection_0_full_read_missing_or_revision_mismatch",
                      self.evaluate(concepts={})["issues"])

    def test_stale_revision_and_stale_concept_require_review(self):
        stale = dict(self.concept, revision="0" * 64)
        self.assertIn("selection_0_full_read_missing_or_revision_mismatch",
                      self.evaluate(concepts={self.path: stale})["issues"])
        expired = read(self.path, self.body.replace("2026-11-01", "2026-10-01"))
        self.assertIn("selection_0_stale_after_passed",
                      self.evaluate(concepts={self.path: expired})["issues"])
        prior = dict(self.selection, expected_revision="0" * 64)
        self.assertIn("selection_0_earlier_hit_revision_stale",
                      self.evaluate(selections=[prior])["issues"])

    def test_conflicting_anchored_interpretations_require_review(self):
        other = read(self.other, self.body.replace("direction is A", "direction is B"))
        second = {"path": self.other, "quote": "The approved direction is B.",
                  "question_key": "direction", "interpretation": "B"}
        result = self.evaluate(selections=[self.selection, second],
                               concepts={self.path: self.concept, self.other: other})
        self.assertIn("interpretation_conflict:direction", result["issues"])
        self.assertEqual(result["evidence_status"], "REVIEW_REQUIRED")

    def test_unanchored_quote_or_noncanonical_material_rejected(self):
        bad = dict(self.selection, quote="fabricated")
        self.assertIn("selection_0_quote_not_in_full_read", self.evaluate(selections=[bad])["issues"])
        inbox = dict(self.selection, path="Brain/inbox/draft.md")
        self.assertIn("selection_0_not_canonical_concept", self.evaluate(selections=[inbox])["issues"])


if __name__ == "__main__":
    unittest.main()
