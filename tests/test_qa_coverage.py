"""Synthetic QA report coverage checks; no service, reviewer or live project."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from aios.qa_coverage import evaluate


def digest(data):
    return hashlib.sha256(data).hexdigest()


class QACoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifacts = {}
        for name, data in (("base.bin", b"synthetic base"),
                           ("candidate.bin", b"synthetic candidate"),
                           ("spec.md", b"synthetic specification"),
                           ("diff.patch", b"synthetic diff"),
                           ("role.md", b"synthetic reviewer role"),
                           ("proof.txt", b"synthetic test observation")):
            (self.root / name).write_bytes(data)
            self.artifacts[name] = {"path": name, "sha256": digest(data)}
        self.request = {"base": self.artifacts["base.bin"],
                        "candidate": self.artifacts["candidate.bin"],
                        "specification": self.artifacts["spec.md"],
                        "diff": self.artifacts["diff.patch"],
                        "role": self.artifacts["role.md"],
                        "developer_id": "synthetic-developer", "scope": "fixture review",
                        "exclusions": ["live deployment"], "rationale": "check the fixed fixture",
                        "known_limitations": ["offline only"],
                        "requirements": [{"id": f"R-{n}", "category": f"QA-{n:02}",
                                          "text": f"Fixture requirement {n}"} for n in range(1, 7)]}
        self.report = {"reviewer_id": "synthetic-reviewer", "modes": ["code_review"],
                       "review_environment": "synthetic disposable fixture",
                       "base_sha256": self.request["base"]["sha256"],
                       "candidate_sha256": self.request["candidate"]["sha256"],
                       "specification_sha256": self.request["specification"]["sha256"],
                       "diff_sha256": self.request["diff"]["sha256"],
                       "role_sha256": self.request["role"]["sha256"],
                       "checks": [{"requirement_id": f"R-{n}", "status": "PASS",
                                   "evidence": [self.artifacts["proof.txt"]]} for n in range(1, 7)],
                       "findings": []}

    def test_all_six_evidenced_is_only_structurally_consistent(self):
        result = evaluate(self.request, self.report, self.root)
        self.assertEqual(result["report_consistency"], "PASS")
        self.assertEqual(result["reported_requirement_outcome"], "ALL_REPORTED_PASS")
        self.assertEqual(result["approval_to_deploy"], "NOT_GRANTED")
        self.assertFalse(result["independent_review_attested"])
        self.assertFalse(result["security_clearance_attested"])

    def test_missing_requirement_stays_unknown(self):
        self.report["checks"].pop()
        result = evaluate(self.request, self.report, self.root)
        self.assertEqual(result["report_consistency"], "UNKNOWN")
        self.assertEqual(result["reported_requirement_outcome"], "UNKNOWN")
        self.assertIn("requirement_uncovered:R-6", result["issues"])

    def test_candidate_change_or_wrong_binding_fails(self):
        (self.root / "candidate.bin").write_bytes(b"different")
        self.report["candidate_sha256"] = "0" * 64
        result = evaluate(self.request, self.report, self.root)
        self.assertEqual(result["report_consistency"], "FAIL")
        self.assertIn("candidate_artifact_fail", result["issues"])
        self.assertIn("candidate_binding_mismatch", result["issues"])

    def test_unsubstantiated_pass_and_unsafe_evidence_fail(self):
        self.report["checks"][0]["evidence"] = []
        self.report["checks"][1]["evidence"] = [{"path": "../outside", "sha256": "a" * 64}]
        result = evaluate(self.request, self.report, self.root)
        self.assertEqual(result["report_consistency"], "FAIL")
        self.assertEqual(result["requirements"]["R-2"]["evidence_status"], "FAIL")
        self.assertIn("unsupported_pass:R-1", result["issues"])

    def test_reported_failure_preserved_and_requires_finding(self):
        self.report["checks"][0] = {"requirement_id": "R-1", "status": "FAIL",
                                    "evidence": [self.artifacts["proof.txt"]],
                                    "reason": "fixture behavior failed"}
        result = evaluate(self.request, self.report, self.root)
        self.assertEqual(result["reported_requirement_outcome"], "FAIL")
        self.assertIn("failure_without_finding:R-1", result["issues"])
        self.report["findings"] = [{"id": "F-1", "requirement_id": "R-1",
                                     "location": "fixture.py:1", "expected": "works", "actual": "fails",
                                     "reproduction": "synthetic proof.txt", "related_security_id": None,
                                     "impact": "fixture output wrong", "owner": "developer",
                                     "disposition": "proposed"}]
        result = evaluate(self.request, self.report, self.root)
        self.assertEqual(result["report_consistency"], "PASS")
        self.assertEqual(result["reported_requirement_outcome"], "FAIL")

    def test_self_review_duplicate_check_and_missing_category_are_visible(self):
        self.report["reviewer_id"] = self.request["developer_id"]
        self.report["checks"].append(copy.deepcopy(self.report["checks"][0]))
        self.request["requirements"] = self.request["requirements"][:-1]
        result = evaluate(self.request, self.report, self.root)
        self.assertEqual(result["report_consistency"], "FAIL")
        self.assertIn("self_review_declared", result["issues"])
        self.assertIn("duplicate_check:R-1", result["issues"])
        self.assertIn("qa_categories_not_requested:QA-06", result["issues"])

    def test_error_unknown_and_not_run_remain_explicit(self):
        for index, status in enumerate(("ERROR", "UNKNOWN", "NOT_RUN")):
            self.report["checks"][index]["status"] = status
            self.report["checks"][index]["reason"] = "fixture was not verified"
            self.report["checks"][index]["evidence"] = []
        result = evaluate(self.request, self.report, self.root)
        self.assertEqual(result["report_consistency"], "PASS")
        self.assertEqual(result["reported_requirement_outcome"], "UNKNOWN")
        self.assertEqual([result["requirements"][f"R-{n}"]["reported_status"] for n in range(1, 4)],
                         ["ERROR", "UNKNOWN", "NOT_RUN"])

    def test_cli_reads_fixed_fixture_and_returns_only_classification(self):
        request_file = self.root / "request.json"
        report_file = self.root / "report.json"
        request_file.write_text(json.dumps(self.request))
        report_file.write_text(json.dumps(self.report))
        completed = subprocess.run(
            [sys.executable, "-B", "-m", "aios.qa_coverage", str(request_file), str(report_file),
             "--evidence-root", str(self.root)], capture_output=True, text=True, check=True,
        )
        output = json.loads(completed.stdout)
        self.assertEqual(output["report_consistency"], "PASS")
        self.assertEqual(output["approval_to_deploy"], "NOT_GRANTED")
        self.assertNotIn("synthetic test observation", completed.stdout)


if __name__ == "__main__":
    unittest.main()
