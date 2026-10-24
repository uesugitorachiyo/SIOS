import unittest

from aios.radar_coverage import check


def source(outcome, **changes):
    value = {
        "url": "https://example.org/current-projects",
        "accessed": "2026-10-21",
        "fact_date": "2026-10-21",
        "outcome": outcome,
        "note": "Public row inspected against prior report",
        "previous_fact": "Bid date October 22",
        "current_fact": "Bid date October 22",
    }
    value.update(changes)
    return value


def record(outcome, **changes):
    value = {
        "routine": "opportunity-radar",
        "run_id": "fixture-not-published",
        "status": "completed",
        "coverage_assessment": "adequate",
        "items": [],
        "sources": [source(outcome)],
    }
    value.update(changes)
    return value


class RadarCoverageTests(unittest.TestCase):
    def test_exact_duplicate_and_report_binding(self):
        checked = check(record("duplicate_unchanged"))
        self.assertEqual(checked["outcomes"]["duplicate_unchanged"], 1)
        self.assertEqual(check(record("duplicate_unchanged"), checked["section"])["status"], "completed")
        with self.assertRaisesRegex(ValueError, "exact generated"):
            check(record("duplicate_unchanged"), "# no outcomes")

    def test_same_url_changed_fact_requires_dated_comparison(self):
        changed = source("changed_excluded", current_fact="Bid date October 28",
                         note="Date differs; official scope unavailable and offer fit unproved")
        checked = check(record("changed_excluded", sources=[changed]))
        self.assertEqual(checked["outcomes"]["changed_excluded"], 1)
        with self.assertRaisesRegex(ValueError, "changed fact"):
            check(record("duplicate_unchanged", sources=[{**changed, "outcome": "duplicate_unchanged"}]))
        with self.assertRaisesRegex(ValueError, "dated fact"):
            check(record("changed_excluded", sources=[{**changed, "fact_date": None}]))
        another_row = {**changed, "current_fact": "Different project bid date October 29"}
        self.assertEqual(check(record("changed_excluded", sources=[changed, another_row]))["source_count"], 2)
        with self.assertRaisesRegex(ValueError, "same observation"):
            check(record("changed_excluded", sources=[changed, changed]))

    def test_unavailable_requires_partial_and_unknown_fact_stays_unknown(self):
        missing = source("unavailable", fact_date=None, note="Source fetch failed")
        with self.assertRaisesRegex(ValueError, "completed requires"):
            check(record("unavailable", sources=[missing]))
        result = check(record("unavailable", status="partial",
                              coverage_assessment="limited", sources=[missing]))
        self.assertIn("fact unknown", result["section"])

    def test_no_qualifying_change_can_be_explicit_with_adequate_coverage(self):
        value = source("checked_no_qualifying_change", note="New agenda reviewed; no buyer trigger")
        self.assertEqual(check(record("checked_no_qualifying_change", sources=[value]))["source_count"], 1)
        with self.assertRaisesRegex(ValueError, "old/undated sources alone"):
            check(record("old_or_undated", sources=[source("old_or_undated")]))

    def test_qualified_change_requires_item_key(self):
        value = source("changed_qualified", current_fact="Named builder selected")
        with self.assertRaisesRegex(ValueError, "item_key"):
            check(record("changed_qualified", sources=[value]))
        value["item_key"] = "signal:builder-selection"
        self.assertEqual(check(record("changed_qualified", sources=[value],
                                      items=["signal:builder-selection"]))["outcomes"]["changed_qualified"], 1)
        with self.assertRaisesRegex(ValueError, "items must equal"):
            check(record("changed_qualified", sources=[value]))


if __name__ == "__main__":
    unittest.main()
