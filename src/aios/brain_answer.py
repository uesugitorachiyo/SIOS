"""Assess already-read Brain evidence without writing or claiming Brain authority."""

from __future__ import annotations

import hashlib
import re
from datetime import date


CONCEPT = re.compile(r"Brain/brain/([a-z0-9-]+)/([a-z0-9-]+)\.md\Z")


def _checked_read(item: object, path: str) -> str | None:
    if not isinstance(item, dict) or item.get("path") != path:
        return None
    text, revision = item.get("text"), item.get("revision")
    if not isinstance(text, str) or not isinstance(revision, str):
        return None
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != revision:
        return None
    return text


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        return {}
    block = text[4:].split("\n---\n", 1)[0]
    result = {}
    for line in block.splitlines():
        if line and not line[0].isspace() and ":" in line:
            key, value = line.split(":", 1)
            result[key] = value.strip().strip('"\'')
    return result


def assess(index_read: object, category_reads: dict[str, object],
           concept_reads: dict[str, object], selections: list[dict], as_of: date) -> dict:
    """Return evidence readiness, never an answer or a permission to act.

    A selection has path, exact quote, question_key and interpretation. The
    interpretation is a human/model annotation; disagreements require review.
    """
    index = _checked_read(index_read, "Brain/brain/index.md")
    issues: list[str] = []
    sources: list[dict[str, str]] = []
    interpretations: dict[str, set[str]] = {}
    if index is None:
        issues.append("root_index_missing_or_revision_mismatch")
    if not selections:
        issues.append("no_selected_evidence")
    for number, selection in enumerate(selections):
        path = selection.get("path") if isinstance(selection, dict) else None
        match = CONCEPT.fullmatch(path) if isinstance(path, str) else None
        if not match or match.group(2) == "index":
            issues.append(f"selection_{number}_not_canonical_concept")
            continue
        category, slug = match.groups()
        category_path = f"Brain/brain/{category}/index.md"
        category_text = _checked_read(category_reads.get(category_path), category_path)
        if index is None or f"{category}/" not in index:
            issues.append(f"selection_{number}_category_not_in_root_index")
        if category_text is None or f"{slug}.md" not in category_text:
            issues.append(f"selection_{number}_concept_not_in_category_index")
        concept = _checked_read(concept_reads.get(path), path)
        if concept is None:
            issues.append(f"selection_{number}_full_read_missing_or_revision_mismatch")
            continue
        prior_revision = selection.get("expected_revision")
        if prior_revision is not None and prior_revision != concept_reads[path]["revision"]:
            issues.append(f"selection_{number}_earlier_hit_revision_stale")
        frontmatter = _frontmatter(concept)
        if frontmatter.get("status") != "stable":
            issues.append(f"selection_{number}_not_stable")
        for field in ("stale_after", "review_due"):
            value = frontmatter.get(field)
            if value:
                try:
                    if date.fromisoformat(value) < as_of:
                        issues.append(f"selection_{number}_{field}_passed")
                except ValueError:
                    issues.append(f"selection_{number}_{field}_invalid")
        if not frontmatter.get("basis") or "sources:" not in concept.split("\n---\n", 1)[0]:
            issues.append(f"selection_{number}_provenance_incomplete")
        if not frontmatter.get("sensitivity"):
            issues.append(f"selection_{number}_sensitivity_unlabeled")
        quote = selection.get("quote")
        if not isinstance(quote, str) or not quote.strip() or quote not in concept:
            issues.append(f"selection_{number}_quote_not_in_full_read")
        key, interpretation = selection.get("question_key"), selection.get("interpretation")
        if not isinstance(key, str) or not key or not isinstance(interpretation, str) or not interpretation:
            issues.append(f"selection_{number}_interpretation_missing")
        else:
            interpretations.setdefault(key, set()).add(interpretation)
        sources.append({"path": path, "revision": concept_reads[path]["revision"],
                        "status": frontmatter.get("status", "unknown"),
                        "sensitivity": frontmatter.get("sensitivity", "unknown"),
                        "basis": frontmatter.get("basis", "unknown"),
                        "verification": "recorded" if "verified:" in concept.split("\n---\n", 1)[0] else "unrecorded"})
    for key, values in interpretations.items():
        if len(values) > 1:
            issues.append(f"interpretation_conflict:{key}")
    return {
        "evidence_status": "REVIEW_REQUIRED" if issues else "EVIDENCE_READY",
        "brain_authority": "UNATTESTED_BY_HELPER",
        "issues": sorted(set(issues)),
        "sources": sources,
        "limit": "Interpretations are annotations; evidence readiness is not an answer or proof of current AIOS Brain authority.",
    }
