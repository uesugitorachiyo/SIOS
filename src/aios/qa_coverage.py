"""Offline QA report coverage check. It never decides release or runs tests.

Request, report, candidate, specification, diff and evidence are fixed inputs.
The existing SIOS receipt hash/path checker is reused for local artifacts.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

from .qa_evidence import _receipt_result


CATEGORIES = {f"QA-{number:02}" for number in range(1, 7)}
STATUSES = {"PASS", "FAIL", "ERROR", "UNKNOWN", "NOT_RUN"}
MODES = {"code_review", "architecture_review", "refactor_assessment"}
ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,119}\Z")
MAX_JSON = 2_000_000


def _descriptor(value: object, root: Path) -> dict:
    return _receipt_result(value, root)


def evaluate(request: dict, report: dict, evidence_root: Path) -> dict:
    """Check fixed inputs and claim coverage; never attest an independent reviewer."""
    if not isinstance(request, dict) or not isinstance(report, dict):
        raise ValueError("Request and report must be objects")
    issues = []
    artifacts = {}
    for name in ("base", "candidate", "specification", "diff", "role"):
        artifacts[name] = _descriptor(request.get(name), evidence_root)
        if artifacts[name]["status"] != "PASS":
            issues.append(f"{name}_artifact_{artifacts[name]['status'].lower()}")
        expected = request.get(name, {}).get("sha256") if isinstance(request.get(name), dict) else None
        if report.get(f"{name}_sha256") != expected:
            issues.append(f"{name}_binding_mismatch")
    if not isinstance(request.get("scope"), str) or not request["scope"].strip():
        issues.append("scope_missing")
    if not isinstance(request.get("exclusions"), list) or not all(isinstance(x, str) for x in request["exclusions"]):
        issues.append("exclusions_missing")
    if not isinstance(request.get("rationale"), str) or not request["rationale"].strip():
        issues.append("rationale_missing")
    if not isinstance(request.get("known_limitations"), list) or not all(isinstance(x, str) for x in request["known_limitations"]):
        issues.append("known_limitations_missing")
    if not isinstance(report.get("review_environment"), str) or not report["review_environment"].strip():
        issues.append("review_environment_missing")
    developer, reviewer = request.get("developer_id"), report.get("reviewer_id")
    if not isinstance(developer, str) or not developer.strip() or not isinstance(reviewer, str) or not reviewer.strip():
        issues.append("reviewer_or_developer_missing")
    elif developer == reviewer:
        issues.append("self_review_declared")
    modes = report.get("modes")
    if (not isinstance(modes, list) or not modes or not all(isinstance(x, str) for x in modes)
            or len(set(modes)) != len(modes) or not set(modes) <= MODES):
        issues.append("review_modes_missing_or_invalid")

    required = request.get("requirements")
    if not isinstance(required, list) or not 1 <= len(required) <= 512:
        raise ValueError("Request must declare 1–512 requirements")
    by_id = {}
    for item in required:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not ID.fullmatch(item["id"]):
            raise ValueError("Invalid requirement ID")
        if item["id"] in by_id:
            raise ValueError("Duplicate request requirement")
        if (not isinstance(item.get("category"), str) or item["category"] not in CATEGORIES
                or not isinstance(item.get("text"), str) or not item["text"].strip()):
            raise ValueError("Requirement needs QA category and text")
        by_id[item["id"]] = item
    unrequested_categories = sorted(CATEGORIES - {item["category"] for item in required})
    if unrequested_categories:
        issues.append("qa_categories_not_requested:" + ",".join(unrequested_categories))

    checks = report.get("checks")
    if not isinstance(checks, list):
        checks = []
        issues.append("checks_missing")
    by_check = {}
    for item in checks:
        if not isinstance(item, dict) or not isinstance(item.get("requirement_id"), str):
            issues.append("invalid_check_record")
            continue
        key = item["requirement_id"]
        if key in by_check:
            issues.append("duplicate_check:" + key)
        elif key not in by_id:
            issues.append("unrequested_check:" + key)
        else:
            by_check[key] = item
    findings = report.get("findings")
    if not isinstance(findings, list):
        findings = []
        issues.append("findings_missing")
    finding_ids = set()
    finding_requirements = set()
    for item in findings:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not ID.fullmatch(item["id"]):
            issues.append("invalid_finding")
            continue
        if item["id"] in finding_ids:
            issues.append("duplicate_finding:" + item["id"])
        finding_ids.add(item["id"])
        requirement_id = item.get("requirement_id")
        if not isinstance(requirement_id, str) or requirement_id not in by_id:
            issues.append("finding_unknown_requirement:" + item["id"])
        else:
            finding_requirements.add(requirement_id)
        if any(not isinstance(item.get(field), str) or not item[field].strip()
               for field in ("location", "expected", "actual", "reproduction", "impact", "owner", "disposition")):
            issues.append("finding_fields_missing:" + item["id"])
        related = item.get("related_security_id")
        if related is not None and not isinstance(related, str):
            issues.append("finding_security_link_invalid:" + item["id"])

    results = {}
    for key in by_id:
        item = by_check.get(key)
        if item is None:
            results[key] = {"reported_status": "UNKNOWN", "evidence_status": "UNKNOWN",
                            "reason": "requirement_check_missing"}
            issues.append("requirement_uncovered:" + key)
            continue
        status = item.get("status")
        if not isinstance(status, str) or status not in STATUSES:
            issues.append("invalid_status:" + key)
            status = "UNKNOWN"
        descriptors = item.get("evidence")
        if not isinstance(descriptors, list):
            descriptors = []
        checks_for_item = [_descriptor(desc, evidence_root) for desc in descriptors]
        evidence_status = "FAIL" if any(x["status"] == "FAIL" for x in checks_for_item) else (
            "UNKNOWN" if not checks_for_item or any(x["status"] == "UNKNOWN" for x in checks_for_item) else "PASS")
        if status == "PASS" and evidence_status != "PASS":
            issues.append("unsupported_pass:" + key)
        if status == "FAIL" and key not in finding_requirements:
            issues.append("failure_without_finding:" + key)
        if status != "PASS" and (not isinstance(item.get("reason"), str) or not item["reason"].strip()):
            issues.append("nonpass_reason_missing:" + key)
        results[key] = {"reported_status": status, "evidence_status": evidence_status,
                        "reason": item.get("reason") if status != "PASS" else "reported_pass_only"}

    severe = any(x.endswith("_fail") or "_binding_mismatch" in x or x.startswith(("duplicate_", "self_review", "unsupported_pass", "failure_without_finding", "invalid_", "unrequested_"))
                 for x in issues)
    consistency = "FAIL" if severe else ("UNKNOWN" if issues else "PASS")
    reported_statuses = [entry["reported_status"] for entry in results.values()]
    reported_outcome = "FAIL" if "FAIL" in reported_statuses else (
        "UNKNOWN" if any(x != "PASS" for x in reported_statuses) else "ALL_REPORTED_PASS")
    return {"report_consistency": consistency, "reported_requirement_outcome": reported_outcome,
            "approval_to_deploy": "NOT_GRANTED", "independent_review_attested": False,
            "security_clearance_attested": False, "artifact_checks": artifacts,
            "requirements": results, "issues": sorted(set(issues)),
            "limit": "Offline local hash and coverage checks cannot judge code quality, authenticate reviewer identity, prove test isolation or approve completion/release."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--evidence-root", required=True, type=Path)
    args = parser.parse_args()
    for path in (args.request, args.report):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JSON:
            parser.error("Input JSON must be an ordinary bounded file")
    request = json.loads(args.request.read_text(encoding="utf-8"))
    report = json.loads(args.report.read_text(encoding="utf-8"))
    print(json.dumps(evaluate(request, report, args.evidence_root), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
