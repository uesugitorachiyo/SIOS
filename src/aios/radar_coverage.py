"""Check Local Lead Radar source outcomes before an ordinary SIOS publish.

This validates the operator's recorded research; it does not fetch sources or
decide whether a buyer signal is semantically relevant.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

OUTCOMES = {
    "duplicate_unchanged",
    "changed_qualified",
    "changed_excluded",
    "checked_no_qualifying_change",
    "old_or_undated",
    "unavailable",
    "verification_conflict",
}
INCOMPLETE = {"unavailable", "verification_conflict"}


def _line(value: object, label: str, limit: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{label} must be nonempty text of at most {limit} characters")
    if any(char in value for char in "\r\n|"):
        raise ValueError(f"{label} must be one line without a table separator")
    return value.strip()


def _day(value: object, label: str) -> str:
    result = _line(value, label, 10)
    try:
        if date.fromisoformat(result).isoformat() != result:
            raise ValueError()
    except ValueError as exc:
        raise ValueError(f"{label} must be YYYY-MM-DD") from exc
    return result


def check(record: dict, report: str | None = None) -> dict:
    if not isinstance(record, dict) or record.get("routine") != "opportunity-radar":
        raise ValueError("routine must be opportunity-radar")
    run_id = _line(record.get("run_id"), "run_id", 120)
    status = record.get("status")
    if status not in {"completed", "partial", "failed"}:
        raise ValueError("status must be completed, partial, or failed")
    coverage = record.get("coverage_assessment")
    if coverage not in {"adequate", "limited"}:
        raise ValueError("coverage_assessment must be adequate or limited")
    sources = record.get("sources")
    if not isinstance(sources, list) or not 1 <= len(sources) <= 6:
        raise ValueError("Record one to six screened sources")
    items = record.get("items")
    if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
        raise ValueError("items must list the exact keys intended for publish")
    lines = ["## Source outcomes", f"- Run {run_id}; status {status}; coverage {coverage}"]
    seen = set()
    qualified = set()
    for number, source in enumerate(sources, 1):
        if not isinstance(source, dict):
            raise ValueError(f"source {number} must be an object")
        url = _line(source.get("url"), f"source {number} URL", 500)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"source {number} URL must be public HTTP(S)")
        outcome = source.get("outcome")
        if outcome not in OUTCOMES:
            raise ValueError(f"source {number} has unknown outcome")
        accessed = _day(source.get("accessed"), f"source {number} accessed")
        note = _line(source.get("note"), f"source {number} note", 180)
        fact_date = source.get("fact_date")
        if fact_date is not None:
            fact_date = _day(fact_date, f"source {number} fact_date")
        if outcome in {"changed_qualified", "changed_excluded", "checked_no_qualifying_change"} and not fact_date:
            raise ValueError(f"source {number} needs a dated fact")
        if outcome in {"changed_qualified", "changed_excluded", "duplicate_unchanged"}:
            prior = _line(source.get("previous_fact"), f"source {number} previous_fact", 180)
            current = _line(source.get("current_fact"), f"source {number} current_fact", 180)
            if outcome == "duplicate_unchanged" and prior != current:
                raise ValueError(f"source {number} changed fact cannot be an unchanged duplicate")
            if outcome.startswith("changed_") and prior == current:
                raise ValueError(f"source {number} unchanged fact cannot be a change")
        if outcome == "changed_qualified":
            qualified.add(_line(source.get("item_key"), f"source {number} item_key", 120))
        # A directory may contain multiple candidate rows. Reuse of its URL is
        # valid; repeating the same observed fact or reason is not.
        observation = (url, source.get("current_fact", note))
        if observation in seen:
            raise ValueError(f"source {number} repeats the same observation")
        seen.add(observation)
        lines.append(f"- {outcome} | {url} | fact {fact_date or 'unknown'}; accessed {accessed} | {note}")
    if len(items) != len(set(items)) or set(items) != qualified:
        raise ValueError("items must equal changed_qualified item keys")
    if status == "completed" and (coverage != "adequate" or any(s["outcome"] in INCOMPLETE for s in sources)):
        raise ValueError("completed requires adequate declared coverage and no unavailable/conflicting source")
    if status == "completed" and all(s["outcome"] == "old_or_undated" for s in sources):
        raise ValueError("old/undated sources alone cannot establish adequate current coverage")
    if any(s["outcome"] in INCOMPLETE for s in sources) and status != "partial":
        raise ValueError("unavailable/conflicting source requires partial status")
    if status == "failed":
        raise ValueError("Use partial for screened research; failed runs have no validated source-outcome report")
    section = "\n".join(lines)
    if report is not None:
        if section not in report:
            raise ValueError("Report must contain the exact generated Source outcomes section")
        if len(report) >= 3500:
            raise ValueError("Report must remain under the Radar 3,500-character limit")
    return {"run_id": run_id, "status": status, "source_count": len(sources),
            "outcomes": {outcome: sum(s["outcome"] == outcome for s in sources) for outcome in sorted(OUTCOMES)},
            "section": section}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path, help="Private JSON source-outcome record for one run")
    parser.add_argument("--report", type=Path, help="Check that the Markdown report includes the generated section")
    args = parser.parse_args()
    result = check(json.loads(args.record.read_text(encoding="utf-8")),
                   args.report.read_text(encoding="utf-8") if args.report else None)
    print(result["section"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
