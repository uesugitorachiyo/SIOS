"""Focused checks for offline Mission QA evidence classification."""

import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aios.qa_evidence import evaluate


NOW = datetime(2026, 10, 20, 18, 0, tzinfo=timezone.utc)
MISSIONS = ("fixture-one", "fixture-two")


def fixture(root: Path):
    observed = (NOW - timedelta(minutes=5)).isoformat()
    paths = [f"Missions/{name}/mission.md" for name in MISSIONS]
    reads = []
    for path in paths:
        body = f"# {path}\n## Next executable action\nReview only.\n"
        reads.append({"path": path, "text": body,
                      "revision": hashlib.sha256(body.encode()).hexdigest(),
                      "instance": "central-sios", "observed_at": observed})
    receipt = root / "receipt.md"
    receipt.write_text("dated prior acceptance\n")
    return {
        "instance": "central-sios",
        "catalog": {"store": "missions", "paths": paths + ["Missions/sios-development/mission.md"],
                    "instance": "central-sios", "observed_at": observed},
        "reads": reads,
        "receipts": [{"path": "receipt.md", "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest()}],
    }


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = fixture(self.root)

    def test_consistent_bundle_is_not_operational_attestation(self):
        report = evaluate(self.bundle, self.root, NOW, list(MISSIONS))
        self.assertEqual(report["bundle_consistency"], "PASS")
        self.assertEqual(report["current_operational_status"], "UNKNOWN")
        self.assertEqual(len(report["missions"]), len(MISSIONS))
        self.assertNotIn("sios-development", report["missions"])

    def test_missing_or_stale_is_unknown(self):
        self.bundle["reads"].pop()
        self.bundle["catalog"]["observed_at"] = (NOW - timedelta(hours=2)).isoformat()
        report = evaluate(self.bundle, self.root, NOW, list(MISSIONS))
        self.assertEqual(report["bundle_consistency"], "UNKNOWN")
        self.assertEqual(report["catalog"]["status"], "UNKNOWN")
        self.assertIn("UNKNOWN", {item["status"] for item in report["missions"].values()})

    def test_tampered_text_or_receipt_is_fail(self):
        self.bundle["reads"][0]["text"] += "changed"
        (self.root / "receipt.md").write_text("changed")
        report = evaluate(self.bundle, self.root, NOW, list(MISSIONS))
        self.assertEqual(report["bundle_consistency"], "FAIL")
        self.assertEqual(report["missions"][MISSIONS[0]]["status"], "FAIL")
        self.assertEqual(report["receipts"]["1"]["status"], "FAIL")

    def test_cross_instance_or_future_time_is_unknown(self):
        self.bundle["reads"][0]["instance"] = "synthetic"
        self.bundle["catalog"]["observed_at"] = (NOW + timedelta(minutes=1)).isoformat()
        report = evaluate(self.bundle, self.root, NOW, list(MISSIONS))
        self.assertEqual(report["catalog"]["status"], "UNKNOWN")
        self.assertEqual(report["missions"][MISSIONS[0]]["status"], "UNKNOWN")

    def test_receipt_traversal_or_link_rejected(self):
        self.bundle["receipts"][0]["path"] = "../receipt.md"
        self.assertEqual(evaluate(self.bundle, self.root, NOW, list(MISSIONS))["receipts"]["1"]["status"], "FAIL")
        (self.root / "link.md").symlink_to(self.root / "receipt.md")
        self.bundle["receipts"][0]["path"] = "link.md"
        self.assertEqual(evaluate(self.bundle, self.root, NOW, list(MISSIONS))["receipts"]["1"]["status"], "FAIL")

    def test_mission_scope_is_explicit_and_validated(self):
        for missions in ([], ["fixture-one", "fixture-one"], ["../private"]):
            with self.assertRaises(ValueError):
                evaluate(self.bundle, self.root, NOW, missions)


if __name__ == "__main__":
    unittest.main()
