"""Read-only, offline consistency check for an exported SIOS Mission QA bundle.

This checks evidence supplied by an authenticated client. It does not authenticate
to SIOS, attest the origin of the export, or qualify a production cutover.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path


MAX_EVIDENCE_AGE = timedelta(hours=1)
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MISSION_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _fresh(value: object, now: datetime) -> bool:
    observed = _time(value)
    return observed is not None and timedelta(0) <= now - observed <= MAX_EVIDENCE_AGE


def _result(status: str, reason: str) -> dict[str, str]:
    return {"status": status, "reason": reason}


def _receipt_result(item: object, root: Path) -> dict[str, str]:
    if not isinstance(item, dict):
        return _result("UNKNOWN", "receipt descriptor missing")
    relative, expected = item.get("path"), item.get("sha256")
    if not isinstance(relative, str) or not isinstance(expected, str) or not SHA256.fullmatch(expected):
        return _result("UNKNOWN", "receipt path or expected SHA-256 missing")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        return _result("FAIL", "receipt path leaves the declared evidence root")
    target = root / path
    if any(part.is_symlink() for part in (root, *[root.joinpath(*path.parts[:i]) for i in range(1, len(path.parts) + 1)])):
        return _result("FAIL", "receipt path contains a symlink")
    if not target.is_file():
        return _result("UNKNOWN", "receipt file unavailable")
    digest_state = hashlib.sha256()
    try:
        with target.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest_state.update(chunk)
    except OSError:
        return _result("UNKNOWN", "receipt could not be read")
    digest = digest_state.hexdigest()
    return _result("PASS" if digest == expected else "FAIL",
                   "receipt bytes match declared hash" if digest == expected else "receipt hash mismatch")


def evaluate(bundle: dict, receipt_root: Path, now: datetime, mission_slugs: list[str]) -> dict:
    """Assess supplied evidence without changing it or disclosing Mission text."""
    if not mission_slugs or len(mission_slugs) > 512 or len(set(mission_slugs)) != len(mission_slugs):
        raise ValueError("Provide a nonempty set of distinct Mission slugs")
    if any(not isinstance(slug, str) or len(slug) > 120 or not MISSION_SLUG.fullmatch(slug)
           for slug in mission_slugs):
        raise ValueError("Invalid Mission slug")
    if now.tzinfo is None:
        raise ValueError("as-of time must include a timezone")
    now = now.astimezone(timezone.utc)
    instance = bundle.get("instance")
    catalog = bundle.get("catalog")
    paths = catalog.get("paths") if isinstance(catalog, dict) else None
    expected_paths = {f"Missions/{slug}/mission.md" for slug in mission_slugs}
    if not isinstance(instance, str) or not instance or instance == "synthetic":
        catalog_check = _result("UNKNOWN", "central instance identity not established")
    elif not isinstance(catalog, dict) or catalog.get("store") != "missions" or not isinstance(paths, list):
        catalog_check = _result("UNKNOWN", "authenticated Mission catalog export missing")
    elif catalog.get("instance") != instance or not _fresh(catalog.get("observed_at"), now):
        catalog_check = _result("UNKNOWN", "catalog instance differs or observation is stale")
    elif not all(isinstance(p, str) for p in paths) or len(paths) != len(set(paths)):
        catalog_check = _result("FAIL", "catalog paths invalid or duplicated")
    elif not expected_paths.issubset(set(paths)):
        catalog_check = _result("UNKNOWN", "one or more specified Mission paths absent")
    else:
        catalog_check = _result("PASS", "specified Mission paths present in fresh catalog")

    exported_reads = bundle.get("reads")
    reads = exported_reads if isinstance(exported_reads, list) else []
    by_path: dict[str, list[dict]] = {}
    for item in reads:
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            by_path.setdefault(item["path"], []).append(item)
    mission_checks = {}
    for slug in mission_slugs:
        path = f"Missions/{slug}/mission.md"
        matches = by_path.get(path, [])
        if len(matches) != 1:
            check = _result("UNKNOWN" if not matches else "FAIL", "full read missing or duplicated")
        else:
            item = matches[0]
            body, revision = item.get("text"), item.get("revision")
            if not isinstance(body, str) or not isinstance(revision, str) or not SHA256.fullmatch(revision):
                check = _result("UNKNOWN", "full text or revision missing")
            elif hashlib.sha256(body.encode("utf-8")).hexdigest() != revision:
                check = _result("FAIL", "full text does not match revision")
            elif item.get("instance") != instance or not _fresh(item.get("observed_at"), now):
                check = _result("UNKNOWN", "read instance differs or observation is stale")
            elif catalog_check["status"] != "PASS" or path not in paths:
                check = _result("UNKNOWN", "full read lacks a fresh matching catalog")
            else:
                check = _result("PASS", "fresh full text matches revision and catalog")
        mission_checks[slug] = check

    descriptors = bundle.get("receipts")
    receipt_checks = {}
    if isinstance(descriptors, list):
        for number, item in enumerate(descriptors, 1):
            receipt_checks[str(number)] = _receipt_result(item, receipt_root)
    if not receipt_checks:
        receipt_checks["none"] = _result("UNKNOWN", "no verification receipt supplied")
    all_checks = [catalog_check, *mission_checks.values(), *receipt_checks.values()]
    verdict = "FAIL" if any(x["status"] == "FAIL" for x in all_checks) else (
        "UNKNOWN" if any(x["status"] == "UNKNOWN" for x in all_checks) else "PASS")
    return {
        "bundle_consistency": verdict,
        "current_operational_status": "UNKNOWN",
        "limitation": "Offline exports and receipt hashes do not prove current authentication, writers, or production behavior.",
        "catalog": catalog_check,
        "missions": mission_checks,
        "receipts": receipt_checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="JSON export of catalog and full read results")
    parser.add_argument("--receipt-root", required=True, type=Path,
                        help="root for relative receipt paths; receipts are read only")
    parser.add_argument("--mission", dest="missions", action="append", required=True,
                        help="Mission slug to assess; repeat for each independently selected Mission")
    parser.add_argument("--as-of", help="timezone-aware ISO-8601 assessment time (for reproducible QA)")
    args = parser.parse_args()
    now = _time(args.as_of) if args.as_of else datetime.now(timezone.utc)
    if now is None:
        parser.error("--as-of must include a timezone")
    bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
    if not isinstance(bundle, dict):
        parser.error("bundle must be a JSON object")
    try:
        report = evaluate(bundle, args.receipt_root, now, args.missions)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
