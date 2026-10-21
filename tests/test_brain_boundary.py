"""Synthetic-only checks for the private Brain transition candidate."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from aios.service import dispatch
from aios.store import ConflictError, Store


DOC = "---\ntitle: Synthetic concept\ntype: concept\nstatus: draft\nbasis: stated\n---\nSynthetic owner decision.\n"
TARGET = "Brain/brain/synthetic-concept.md"
SOURCES = [{"resource": "synthetic://owner-decision", "title": "Synthetic fixture"}]


class BrainBoundary(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "private-state")
        self.proposal = dispatch(
            self.store, "brain_propose",
            {"target": TARGET, "content": DOC, "request_id": "synthetic-proposal-1",
             "sources": SOURCES, "basis": "stated", "expected_revision": None},
            actor="proposer", scopes={"brain:propose"},
        )["result"]

    def test_candidate_read_is_reviewable_and_clearly_not_canonical(self):
        proposal_id = self.proposal["proposal_id"]
        result = dispatch(self.store, "read", {"relative": self.proposal["path"]},
                          actor="reader", scopes={"read"})
        self.assertEqual(result["text"], DOC)
        self.assertEqual(result["authority_status"], "pending_or_source_not_canonical")
        self.assertIn("proposal_id", dispatch(self.store, "read",
                     {"relative": f"Runs/brain-proposals/{proposal_id}.json"},
                     actor="reader", scopes={"read"})["text"])

    def test_review_available_to_reader_without_apply(self):
        args = {"proposal_id": self.proposal["proposal_id"]}
        reviewed = dispatch(self.store, "brain_review", args, actor="reader", scopes={"read"})
        self.assertFalse(reviewed["stale"])
        self.assertEqual(reviewed["proposal_revision"], hashlib.sha256(DOC.encode()).hexdigest())

    def test_ordinary_reads_remain_available(self):
        self.store._operation("synthetic-ordinary-read", "local", {"operation": "fixture"},
                              lambda: ([("Memory/synthetic-note.md", "Ordinary synthetic note\n"),
                                        (TARGET, DOC)], {}))
        for path in ("Memory/synthetic-note.md", TARGET):
            result = dispatch(self.store, "read", {"relative": path}, actor="reader", scopes={"read"})
            self.assertTrue(result["text"])
            if path == TARGET:
                self.assertEqual(result["authority_status"], "canonical")
            else:
                self.assertNotIn("authority_status", result)

    def test_apply_requires_owner_scope_and_exact_reviewed_hash(self):
        args = {"proposal_id": self.proposal["proposal_id"],
                "proposal_revision": self.proposal["proposal_revision"],
                "request_id": "synthetic-approval-1"}
        with self.assertRaises(PermissionError):
            dispatch(self.store, "brain_apply", args, actor="proposer", scopes={"brain:propose"})
        with self.assertRaises(ConflictError):
            dispatch(self.store, "brain_apply", {**args, "proposal_revision": "0" * 64},
                     actor="local", scopes={"brain:approve"})
        approved = dispatch(self.store, "brain_apply", args, actor="local", scopes={"brain:approve"})
        self.assertEqual(approved["result"]["revision"], self.proposal["proposal_revision"])
        self.assertEqual(dispatch(self.store, "read", {"relative": TARGET}, actor="reader", scopes={"read"})["text"], DOC)
        self.assertEqual(dispatch(self.store, "brain_apply", args, actor="local", scopes={"brain:approve"}), approved)

    def test_stale_base_rejected(self):
        second = DOC.replace("Synthetic owner decision.", "Different synthetic decision.")
        self.store._operation("synthetic-other-write", "local", {"operation": "synthetic-competing-write"},
                              lambda: ([(TARGET, second)], {"path": TARGET}))
        args = {"proposal_id": self.proposal["proposal_id"],
                "proposal_revision": self.proposal["proposal_revision"],
                "request_id": "synthetic-stale-approval"}
        reviewed = dispatch(self.store, "brain_review", {"proposal_id": self.proposal["proposal_id"]},
                            actor="reader", scopes={"read"})
        self.assertTrue(reviewed["stale"])
        with self.assertRaises(ConflictError):
            dispatch(self.store, "brain_apply", args, actor="local", scopes={"brain:approve"})
        self.assertEqual(dispatch(self.store, "read", {"relative": TARGET}, actor="reader", scopes={"read"})["text"], second)


if __name__ == "__main__":
    unittest.main()
