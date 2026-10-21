"""Synthetic-only three-file Brain proposal/approval checks."""
import tempfile
import unittest
from pathlib import Path

from aios.service import dispatch
from aios.store import ConflictError, Store, ValidationError


TARGET = "Brain/brain/category/synthetic-concept.md"
INDEX = "Brain/brain/category/index.md"
LOG = "Brain/brain/log.md"
CONCEPT = "---\ntitle: Synthetic concept\ntype: concept\nstatus: draft\nbasis: stated\n---\nSynthetic decision.\n"
OLD_INDEX = "# Synthetic category\n"
NEW_INDEX = OLD_INDEX + "\n- [Synthetic concept](/category/synthetic-concept.md)\n"
OLD_LOG = "# Brain Update Log\n"
NEW_LOG = OLD_LOG + "\n## 2026-10-20\n\nSynthetic concept proposed.\n"


class BrainBundle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "private-state")
        self.store._operation("fixture-index-log", "local", {"operation": "synthetic-seed"},
                              lambda: ([(INDEX, OLD_INDEX), (LOG, OLD_LOG)], {}))
        index_rev = self.store.read(INDEX)["revision"]
        log_rev = self.store.read(LOG)["revision"]
        concept = dispatch(self.store, "brain_propose",
                           {"target": TARGET, "content": CONCEPT,
                            "sources": [{"resource": "synthetic://source", "title": "Fixture"}],
                            "basis": "stated", "expected_revision": None,
                            "request_id": "fixture-concept-proposal"},
                           actor="proposer", scopes={"brain:propose"})["result"]
        self.bundle = dispatch(self.store, "brain_bundle_propose",
                               {"concept_proposal_id": concept["proposal_id"],
                                "index_content": NEW_INDEX, "log_content": NEW_LOG,
                                "index_expected_revision": index_rev,
                                "log_expected_revision": log_rev,
                                "request_id": "fixture-bundle-proposal"},
                               actor="proposer", scopes={"brain:propose"})["result"]

    def test_review_and_three_file_owner_apply(self):
        review = dispatch(self.store, "brain_bundle_review", {"bundle_id": self.bundle["bundle_id"]},
                          actor="reader", scopes={"read"})
        self.assertFalse(review["stale"])
        self.assertIn("Synthetic concept", review["index_diff"])
        self.assertIn("2026-10-20", review["log_diff"])
        args = {"bundle_id": self.bundle["bundle_id"],
                "bundle_revision": self.bundle["bundle_revision"],
                "request_id": "fixture-bundle-approval"}
        with self.assertRaises(PermissionError):
            dispatch(self.store, "brain_bundle_apply", args, actor="reader", scopes={"read"})
        with self.assertRaises(ConflictError):
            dispatch(self.store, "brain_bundle_apply", {**args, "bundle_revision": "0" * 64},
                     actor="local", scopes={"brain:approve"})
        applied = dispatch(self.store, "brain_bundle_apply", args, actor="local", scopes={"brain:approve"})
        self.assertEqual(dispatch(self.store, "brain_bundle_apply", args, actor="local", scopes={"brain:approve"}), applied)
        for path, text in ((TARGET, CONCEPT), (INDEX, NEW_INDEX), (LOG, NEW_LOG)):
            self.assertEqual(self.store.read(path)["text"], text)
        self.assertIn("human:local", self.store.read(applied["result"]["audit_path"])["text"])

    def test_stale_index_refuses_all_three_writes(self):
        self.store._operation("fixture-competing-index", "local", {"operation": "synthetic-index-change"},
                              lambda: ([(INDEX, OLD_INDEX + "\nOther change.\n")], {}))
        review = dispatch(self.store, "brain_bundle_review", {"bundle_id": self.bundle["bundle_id"]},
                          actor="reader", scopes={"read"})
        self.assertTrue(review["stale"])
        with self.assertRaises(ConflictError):
            dispatch(self.store, "brain_bundle_apply",
                     {"bundle_id": self.bundle["bundle_id"], "bundle_revision": self.bundle["bundle_revision"],
                      "request_id": "fixture-stale-bundle-approval"},
                     actor="local", scopes={"brain:approve"})
        self.assertIsNone(self.store._bytes(TARGET))
        self.assertEqual(self.store.read(LOG)["text"], OLD_LOG)

    def test_broken_index_or_rewritten_log_cannot_be_proposed(self):
        concept_id = dispatch(self.store, "brain_bundle_review", {"bundle_id": self.bundle["bundle_id"]},
                              actor="reader", scopes={"read"})["bundle_id"]
        self.assertEqual(concept_id, self.bundle["bundle_id"])
        from aios.brain_bundle import _check_index, _check_log
        with self.assertRaises(ValidationError):
            _check_index(TARGET, OLD_INDEX)
        with self.assertRaises(ValidationError):
            _check_log(OLD_LOG + "## 2026-09-30\nOld history\n",
                       "# Brain Update Log\n## 2026-10-20\nReplacement\n")

    def test_owner_cli_bundle_commands_are_distinct(self):
        from aios.cli import parser, operation_params
        index_file = Path(self.temp.name) / "index.txt"
        log_file = Path(self.temp.name) / "log.txt"
        index_file.write_text(NEW_INDEX, encoding="utf-8")
        log_file.write_text(NEW_LOG, encoding="utf-8")
        propose = parser().parse_args(["--config", "synthetic.json", "brain-bundle-propose",
                                       "--concept-proposal", "0" * 64,
                                       "--index-file", str(index_file), "--log-file", str(log_file),
                                       "--index-expected-revision", "1" * 64,
                                       "--log-expected-revision", "2" * 64,
                                       "--request-id", "synthetic-cli-bundle"])
        operation, params = operation_params(propose)
        self.assertEqual(operation, "brain_bundle_propose")
        self.assertEqual(params["index_content"], NEW_INDEX)
        self.assertEqual(params["log_content"], NEW_LOG)
        review = parser().parse_args(["--config", "synthetic.json", "brain-bundle-review",
                                      "--bundle", self.bundle["bundle_id"]])
        self.assertEqual(operation_params(review),
                         ("brain_bundle_review", {"bundle_id": self.bundle["bundle_id"]}))
        approve = parser().parse_args(["--config", "synthetic.json", "brain-bundle-approve",
                                       "--bundle", self.bundle["bundle_id"],
                                       "--request-id", "synthetic-owner-request"])
        self.assertEqual(approve.command, "brain-bundle-approve")

    def test_tampered_bundle_candidate_fails_closed(self):
        path = self.store._path(f"Brain/working/candidates/{self.bundle['bundle_id']}-index.md")
        path.write_text(NEW_INDEX + "Changed after proposal.\n", encoding="utf-8")
        with self.assertRaises(ConflictError):
            dispatch(self.store, "brain_bundle_review", {"bundle_id": self.bundle["bundle_id"]},
                     actor="reader", scopes={"read"})
        with self.assertRaises(ConflictError):
            dispatch(self.store, "brain_bundle_apply",
                     {"bundle_id": self.bundle["bundle_id"], "bundle_revision": self.bundle["bundle_revision"],
                      "request_id": "fixture-tamper-approval"},
                     actor="local", scopes={"brain:approve"})
        self.assertIsNone(self.store._bytes(TARGET))


if __name__ == "__main__":
    unittest.main()
