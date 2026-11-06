"""Bounded, read-only memory search resolver with full-source verification.

Designed for SIOS registered client environments where some original Memory notes
may be missing from the SQLite FTS search_index table, while remaining fully readable
in the Store and discoverable via exact stem queries or verified reference notes.

Guarantees & Constraints:
- Bounded read-only client helper: directly initiates only caller-provided search_fn
  and read_fn; performs no direct writes to Store or filesystem. However, underlying
  Store.read and search operations may hold write locks or trigger server-side
  recovery of pending operations and background index synchronization.
- Anti-fragment / Anti-hallucination: Search hit fragments (snippets) are NEVER
  treated as proof of authority, relevance, or supersession. Both the referring
  note and the referenced original note must be full-read and verified for
  SHA-256 revision integrity and structured aios-selective-memory-v1 JSON schema
  before determining their relationship.
- Fail-closed: invalid paths, hash mismatches, read errors, or schema invalidities
  are recorded truthfully in warnings and never promoted to verified results.
  Invalid search response formats raise ValueError and are never silently treated
  as empty results.
- Bounded read budget: all read_fn invocations (for referring notes, target
  originals, or exact-stem fallbacks, whether successful or failed) count against
  the read budget.
- Truthful provenance: clearly distinguishes indexed hits from verified direct-stem
  resolutions and verified pointer-dereferenced originals, while explicitly noting
  that index coverage and current operational state remain unknown, and that
  underlying read and search operations may invoke server recovery or index sync.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable

STEM_PATTERN = re.compile(r"^selective-([0-9a-f]{64})$")
EXACT_QUERY_PATTERN = re.compile(r"^(?:Memory/notes/)?(selective-[0-9a-f]{64})(?:\.md)?$")
REFERENCE_PATTERN = re.compile(r"(?:Memory/notes/)?(selective-[0-9a-f]{64})(?:\.md)?")
VALID_RELATIVE_PATH = re.compile(r"^Memory/notes/(selective-[0-9a-f]{64})\.md$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
MARKER = "<!-- aios-selective-memory-v1 -->"
BLOCK = re.compile(r"(?:\A|\n)```json\s*\n(.*?)\n```(?:\n|\Z)", re.DOTALL)

MAX_READS_DEFAULT = 6
MAX_REFS_PER_NOTE = 2
MAX_SOURCE_BYTES = 131072
REQUIRED_SELECTIVE_FIELDS = ("schema", "meaning_id", "claim", "conditions", "evidence_status")


def _ordinary_note_path(path: Any) -> bool:
    """Accept only normalized Memory note paths supplied by the index."""
    if not isinstance(path, str) or not path.startswith('Memory/notes/') or not path.endswith('.md'):
        return False
    try:
        length = len(path.encode('utf-8'))
    except UnicodeError:
        return False
    if length > 1024 or '\\' in path or '\x00' in path:
        return False
    parts = path.split('/')
    return len(parts) >= 3 and all(part not in ('', '.', '..') for part in parts)


def _unique_pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate key: {key}")
        obj[key] = value
    return obj


def _validate_and_hash_source(
    source: dict[str, Any],
    expected_path: str,
) -> tuple[bool, str | None, str | None]:
    """Validate that read_fn response matches path and has matching SHA-256 revision."""
    if not isinstance(source, dict):
        return False, None, "source_not_dict"
    path = source.get("path")
    revision = source.get("revision")
    text = source.get("text")
    if path != expected_path:
        return False, None, f"path_mismatch_expected_{expected_path}_got_{path}"
    if not isinstance(revision, str) or not HEX64.fullmatch(revision):
        return False, None, "invalid_revision_format"
    if not isinstance(text, str):
        return False, None, "text_not_string"

    raw = text.encode("utf-8")
    if len(raw) > MAX_SOURCE_BYTES:
        return False, None, "source_exceeds_byte_limit"

    actual_hash = hashlib.sha256(raw).hexdigest()
    if actual_hash != revision:
        return False, None, f"hash_mismatch_expected_{revision}_got_{actual_hash}"

    return True, actual_hash, None


def _validate_and_parse_source(
    source: dict[str, Any],
    expected_path: str,
) -> tuple[bool, str | None, dict[str, Any] | None, str | None]:
    """Validate that read_fn response matches path, has valid SHA-256 hash, and strict selective schema."""
    ok, actual_hash, err = _validate_and_hash_source(source, expected_path)
    if not ok or not actual_hash:
        return False, None, None, err

    text = source["text"]
    # Strict schema validation for selective memory notes
    if not text.startswith("---\ntitle: ") or MARKER not in text:
        return False, None, None, "missing_selective_memory_marker"

    after_marker = text.split(MARKER, 1)[1]
    blocks = BLOCK.findall(after_marker)
    if len(blocks) != 1:
        return False, None, None, "invalid_or_multiple_json_blocks"

    try:
        record = json.loads(blocks[0], object_pairs_hook=_unique_pairs)
        if not isinstance(record, dict) or record.get("schema") != "aios-selective-memory-v1":
            return False, None, None, "invalid_schema_declaration"
        for field in REQUIRED_SELECTIVE_FIELDS:
            if field not in record:
                return False, None, None, f"missing_required_field_{field}"
        if not isinstance(record["claim"], str) or not record["claim"].strip():
            return False, None, None, "invalid_or_empty_claim"
        if not isinstance(record["conditions"], list) or any(not isinstance(c, str) for c in record["conditions"]):
            return False, None, None, "invalid_conditions_list"
    except (ValueError, TypeError, RecursionError) as exc:
        return False, None, None, f"json_parse_error_{type(exc).__name__}"

    return True, actual_hash, record, None


def _extract_references_from_text(text: str, exclude_stems: set[str], limit: int) -> list[str]:
    """Extract distinct selective note stems referenced in verified full text."""
    if not isinstance(text, str) or not text:
        return []

    matches = REFERENCE_PATTERN.findall(text)
    refs: list[str] = []
    seen: set[str] = set()
    for stem in matches:
        if stem in exclude_stems or stem in seen:
            continue
        seen.add(stem)
        refs.append(stem)
        if len(refs) >= limit:
            break
    return refs


def resolve_search_verified(
    query: str,
    search_fn: Callable[[str], dict[str, Any]],
    read_fn: Callable[[str], dict[str, Any]],
    *,
    max_reads: int = MAX_READS_DEFAULT,
) -> dict[str, Any]:
    """Execute search with full-source verification of hits and referenced originals.

    Args:
        query: Search query text.
        search_fn: Callable executing memory_index_search(query).
        read_fn: Callable executing read(relative=path).
        max_reads: Total maximum number of read_fn calls permitted across referring
            notes, dereferenced targets, and exact-stem fallbacks.

    Returns:
        Structured search results with fully verified sources, truthful provenance,
        derived supersession relations, and explicit coverage warnings.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be non-empty text")
    if type(max_reads) is not int or not 0 <= max_reads <= 20:
        raise ValueError("max_reads must be an integer from 0 to 20")

    cleaned_query = query.strip()
    warnings: list[dict[str, Any]] = []
    verified_results: list[dict[str, Any]] = []
    seen_stems: set[str] = set()
    seen_paths: set[str] = set()
    attempted_paths: set[str] = set()
    read_attempts = 0
    referring_verified_count = 0
    targets_verified_count = 0
    exact_stem_resolved = False
    # Existing exact_path_resolved reports the direct-path fallback only.
    exact_path_resolved = False
    exact_path_verified = False

    # Check if query is an exact-stem search (e.g. 'selective-1b4c...')
    exact_match = EXACT_QUERY_PATTERN.fullmatch(cleaned_query)
    target_stem: str | None = exact_match.group(1) if exact_match else None

    # Step 1: Upstream indexed search
    raw_search = search_fn(cleaned_query)
    if not isinstance(raw_search, dict):
        raise ValueError(f"invalid search response: expected dict, got {type(raw_search).__name__}")

    payload = raw_search.get("result", raw_search)
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("invalid search response: expected results list")

    hits = payload["results"]
    if type(payload.get("has_more")) is bool and payload["has_more"]:
        warnings.append({"type": "index_results_limited", "reason": "has_more_is_true"})

    # Map candidate hits from index
    indexed_candidates: list[dict[str, Any]] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        read_path = hit.get("read_path")
        if isinstance(read_path, str) and VALID_RELATIVE_PATH.fullmatch(read_path):
            m = VALID_RELATIVE_PATH.fullmatch(read_path)
            stem = m.group(1) if m else None
            if stem:
                seen_stems.add(stem)
                seen_paths.add(read_path)
        indexed_candidates.append(hit)

    # Honor a precise request before spending the bounded budget on other hits.
    # Keep the remaining index order unchanged and do not fabricate candidates.
    requested_path = (f"Memory/notes/{target_stem}.md" if target_stem else
                      cleaned_query if _ordinary_note_path(cleaned_query) else None)
    if requested_path:
        indexed_candidates.sort(key=lambda hit: hit.get("read_path") != requested_path)

    # An explicit ordinary path is a bounded direct read, even if FTS omitted it.
    # Never infer a path from a natural-language query or an index snippet.
    if (_ordinary_note_path(cleaned_query) and not VALID_RELATIVE_PATH.fullmatch(cleaned_query)
            and not any(hit.get('read_path') == cleaned_query for hit in indexed_candidates)):
        if read_attempts >= max_reads:
            warnings.append({'type': 'read_budget_exhausted', 'path': cleaned_query})
        else:
            attempted_paths.add(cleaned_query)
            read_attempts += 1
            try:
                source = read_fn(cleaned_query)
                ok, digest, err = _validate_and_hash_source(source, cleaned_query)
                if ok and digest:
                    verified_results.append({'path': cleaned_query, 'retrieval_method': 'direct_path_read',
                        'status': 'verified_exact_source', 'revision': digest, 'text': source['text'],
                        'verification_basis': 'full_read_sha256_verified', 'provenance': 'store_source_read'})
                    targets_verified_count += 1
                    exact_path_resolved = True
                    exact_path_verified = True
                else:
                    warnings.append({'type': 'exact_path_read_invalid', 'path': cleaned_query, 'reason': err})
            except Exception as exc:
                if type(exc).__name__ == 'ToolError':
                    raise
                warnings.append({'type': 'exact_path_read_error', 'path': cleaned_query,
                                 'reason': f'read_error_{type(exc).__name__}'})

    # Step 2: Exact-stem resolution fallback
    # If the user queried an exact stem and it was omitted from indexed hits:
    if target_stem and target_stem not in seen_stems:
        target_path = f"Memory/notes/{target_stem}.md"
        if read_attempts < max_reads and target_path not in attempted_paths:
            attempted_paths.add(target_path)
            read_attempts += 1
            try:
                source = read_fn(target_path)
                ok, digest, parsed_json, err = _validate_and_parse_source(source, target_path)
                if ok and digest and parsed_json:
                    exact_entry = {
                        "path": target_path,
                        "stem": target_stem,
                        "retrieval_method": "direct_stem_read",
                        "status": "verified_exact_source",
                        "revision": digest,
                        "structured_record": parsed_json,
                        "text": source.get("text"),
                        "verification_basis": "full_read_sha256_verified",
                        "provenance": "store_source_read",
                    }
                    verified_results.append(exact_entry)
                    seen_stems.add(target_stem)
                    seen_paths.add(target_path)
                    exact_stem_resolved = True
                    if cleaned_query == target_path:
                        exact_path_verified = True
                    targets_verified_count += 1
                else:
                    warnings.append({
                        "type": "exact_stem_read_invalid",
                        "path": target_path,
                        "reason": err or "validation_failed",
                    })
            except Exception as exc:
                if type(exc).__name__ == "ToolError":
                    raise
                warnings.append({
                    "type": "exact_stem_read_error",
                    "path": target_path,
                    "reason": f"read_error_{type(exc).__name__}",
                })

    # Step 3: Full-read and verification of referring sources and referenced originals
    for hit in indexed_candidates:
        read_path = hit.get("read_path")
        if _ordinary_note_path(read_path) and not VALID_RELATIVE_PATH.fullmatch(read_path):
            if read_path in attempted_paths:
                continue
            if read_attempts >= max_reads:
                warnings.append({"type": "read_budget_exhausted", "path": read_path})
                continue
            attempted_paths.add(read_path)
            read_attempts += 1
            try:
                source = read_fn(read_path)
                ok, digest, err = _validate_and_hash_source(source, read_path)
                if not ok or not digest:
                    warnings.append({"type": "indexed_source_invalid", "path": read_path, "reason": err})
                    continue
                verified_results.append({"path": read_path, "title": hit.get("title"),
                    "retrieval_method": "indexed_search", "status": "verified_indexed_source",
                    "revision": digest, "text": source["text"],
                    "verification_basis": "full_read_sha256_verified", "provenance": "fts_index_and_store_read"})
                if read_path == cleaned_query:
                    exact_path_verified = True
                referring_verified_count += 1
            except Exception as exc:
                if type(exc).__name__ == "ToolError":
                    raise
                warnings.append({"type": "indexed_source_read_error", "path": read_path,
                                 "reason": f"read_error_{type(exc).__name__}"})
            continue
        if not isinstance(read_path, str) or not VALID_RELATIVE_PATH.fullmatch(read_path):
            warnings.append({"type": "invalid_hit_path", "path": str(read_path)})
            continue

        m = VALID_RELATIVE_PATH.fullmatch(read_path)
        hit_stem = m.group(1) if m else None

        # Verify the referring note itself via full read
        referring_source: dict[str, Any] | None = None
        referring_json: dict[str, Any] | None = None
        referring_digest: str | None = None

        if read_path not in attempted_paths:
            if read_attempts >= max_reads:
                warnings.append({"type": "read_budget_exhausted", "path": read_path})
            else:
                attempted_paths.add(read_path)
                read_attempts += 1
                try:
                    source = read_fn(read_path)
                    ok, digest, parsed_json, err = _validate_and_parse_source(source, read_path)
                    if ok and digest and parsed_json:
                        referring_source = source
                        referring_digest = digest
                        referring_json = parsed_json
                        referring_verified_count += 1
                        if read_path == cleaned_query:
                            exact_path_verified = True
                    else:
                        warnings.append({
                            "type": "referring_source_invalid",
                            "path": read_path,
                            "reason": err or "validation_failed",
                        })
                except Exception as exc:
                    if type(exc).__name__ == "ToolError":
                        raise
                    warnings.append({
                        "type": "referring_source_read_error",
                        "path": read_path,
                        "reason": f"read_error_{type(exc).__name__}",
                    })

        hit_entry = {
            "path": read_path,
            "stem": hit_stem,
            "title": hit.get("title"),
            "score": hit.get("score"),
            "retrieval_method": "indexed_search",
            "status": "verified_referring_source" if referring_digest else "unverified_indexed_hit",
            "revision": referring_digest,
            "structured_record": referring_json,
            "text": referring_source.get("text") if referring_source else None,
            "verification_basis": "full_read_sha256_verified" if referring_digest else "unverified_index_snippet",
            "provenance": "fts_index_and_store_read" if referring_digest else "fts_index_only",
        }
        verified_results.append(hit_entry)

        # Extract references only from the VERIFIED full text of the referring note
        if not referring_source or not referring_source.get("text"):
            continue

        full_text = referring_source["text"]
        exclude = set(seen_stems)
        if hit_stem:
            exclude.add(hit_stem)

        ref_stems = _extract_references_from_text(full_text, exclude_stems=exclude, limit=MAX_REFS_PER_NOTE)

        for ref_stem in ref_stems:
            ref_path = f"Memory/notes/{ref_stem}.md"
            if ref_path in seen_paths or ref_stem in seen_stems or ref_path in attempted_paths:
                continue

            if read_attempts >= max_reads:
                warnings.append({"type": "read_budget_exhausted", "path": ref_path, "pointer": read_path})
                break

            attempted_paths.add(ref_path)
            read_attempts += 1

            try:
                target_source = read_fn(ref_path)
                ok, target_digest, target_json, err = _validate_and_parse_source(target_source, ref_path)
                if ok and target_digest and target_json:
                    # Determine relationship strictly from verified full JSON records
                    relationship = "verified_mention_in_referring_source"
                    if referring_json and referring_json.get("supersedes") == ref_stem:
                        relationship = "referring_source_supersedes_target"
                    elif target_json and target_json.get("supersedes") == hit_stem:
                        relationship = "target_supersedes_referring_source"

                    deref_entry = {
                        "path": ref_path,
                        "stem": ref_stem,
                        "retrieval_method": "dereferenced_from_pointer",
                        "status": "resolved_target_original",
                        "pointer_origin_path": read_path,
                        "relationship_to_pointer": relationship,
                        "revision": target_digest,
                        "structured_record": target_json,
                        "text": target_source.get("text"),
                        "verification_basis": "full_read_sha256_verified",
                        "provenance": "pointer_dereference_read",
                    }
                    verified_results.append(deref_entry)
                    seen_stems.add(ref_stem)
                    seen_paths.add(ref_path)
                    targets_verified_count += 1
                else:
                    warnings.append({
                        "type": "pointer_target_invalid",
                        "path": ref_path,
                        "pointer": read_path,
                        "reason": err or "validation_failed",
                    })
            except Exception as exc:
                if type(exc).__name__ == "ToolError":
                    raise
                warnings.append({
                    "type": "pointer_target_read_error",
                    "path": ref_path,
                    "pointer": read_path,
                    "reason": f"read_error_{type(exc).__name__}",
                })

    # Report declared relationships even when both sources were indexed hits.
    # Only full-source verified records participate; snippets cannot establish
    # a link. This does not discover successors outside the returned results.
    verified_by_stem = {
        item['stem']: item for item in verified_results
        if isinstance(item.get('stem'), str)
        and isinstance(item.get('structured_record'), dict)
        and item.get('verification_basis') == 'full_read_sha256_verified'
        and isinstance(item.get('revision'), str)
    }
    declared_corrections = []
    for stem, item in verified_by_stem.items():
        predecessor = item['structured_record'].get('supersedes')
        if (isinstance(predecessor, str) and predecessor != stem
                and predecessor in verified_by_stem):
            old = verified_by_stem[predecessor]
            declared_corrections.append({
                'successor_path': item['path'], 'successor_revision': item['revision'],
                'predecessor_path': old['path'], 'predecessor_revision': old['revision'],
                'basis': 'declared_supersedes_in_verified_full_source',
            })

    # Truthful dynamic notice
    if read_attempts == 0:
        notice = (
            "No full reads were performed (budget=0). Results contain unverified index snippets only. "
            "Always verify sources using authenticated read. Read and search calls may have server-side "
            "recovery or index synchronization effects. Index coverage and current operational status remain unknown."
        )
    else:
        notice = (
            f"Index snippets are navigation only. Verified {referring_verified_count} referring note(s) "
            f"and {targets_verified_count} target original(s) via authenticated full read and SHA-256 revision match. "
            f"Unverified hits remain index snippets only. Supersession is established only from declared full JSON records. "
            f"Read and search calls may have server-side recovery or index synchronization effects. "
            f"Index coverage and current operational status remain unknown."
        )

    return {
        "query": cleaned_query,
        "results": verified_results,
        "declared_corrections": declared_corrections,
        "total_results": len(verified_results),
        "referring_verified_count": referring_verified_count,
        "targets_verified_count": targets_verified_count,
        "exact_stem_resolved": exact_stem_resolved,
        "exact_path_resolved": exact_path_resolved,
        "exact_path_verified": exact_path_verified,
        "read_budget": {
            "max_reads": max_reads,
            "read_attempts": read_attempts,
        },
        "warnings": warnings,
        "index_coverage": "unknown",
        "current_state": "unknown",
        "notice": notice,
    }


def resolve_search(
    query: str,
    search_fn: Callable[[str], dict[str, Any]],
    read_fn: Callable[[str], dict[str, Any]],
    *,
    max_dereferences: int = 5,
) -> dict[str, Any]:
    """Preserve exact compatibility contract from feature/search-pointer-resolution-reviewed-v2."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be non-empty text")
    if type(max_dereferences) is not int or not 0 <= max_dereferences <= 20:
        raise ValueError("max_dereferences must be an integer from 0 to 20")

    cleaned_query = query.strip()
    warnings: list[dict[str, str]] = []
    results: list[dict[str, Any]] = []
    seen_stems: set[str] = set()
    seen_paths: set[str] = set()
    dereferenced_count = 0
    read_attempts = 0
    attempted_paths: set[str] = set()
    exact_stem_resolved = False

    exact_match = EXACT_QUERY_PATTERN.fullmatch(cleaned_query)
    target_stem: str | None = exact_match.group(1) if exact_match else None

    raw_search = search_fn(cleaned_query)
    if not isinstance(raw_search, dict):
        raise ValueError("invalid search response")

    payload = raw_search.get("result", raw_search)
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("invalid search response")

    hits = payload["results"]
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        read_path = hit.get("read_path")
        if isinstance(read_path, str) and VALID_RELATIVE_PATH.fullmatch(read_path):
            m = VALID_RELATIVE_PATH.fullmatch(read_path)
            stem = m.group(1) if m else None
            if stem:
                seen_stems.add(stem)
                seen_paths.add(read_path)
        results.append(hit)

    if target_stem:
        target_path = f"Memory/notes/{target_stem}.md"
        if target_stem not in seen_stems and read_attempts < max_dereferences:
            read_attempts += 1
            attempted_paths.add(target_path)
            try:
                source = read_fn(target_path)
                ok, digest, err = _validate_and_hash_source(source, target_path)
                if ok and digest:
                    exact_hit = {
                        "retrieval_method": "direct_stem_read",
                        "read_path": target_path,
                        "title": target_stem,
                        "score": 0.0,
                        "revision": digest,
                        "text": source.get("text"),
                        "source_provenance": "store_exact_read",
                        "status": "resolved_exact_source",
                    }
                    results.insert(0, exact_hit)
                    seen_stems.add(target_stem)
                    seen_paths.add(target_path)
                    exact_stem_resolved = True
                    dereferenced_count += 1
                else:
                    warnings.append({
                        "type": "exact_stem_read_invalid",
                        "path": target_path,
                        "reason": err or "validation_failed",
                    })
            except Exception as exc:
                if type(exc).__name__ == "ToolError":
                    raise
                warnings.append({
                    "type": "exact_stem_read_error",
                    "path": target_path,
                    "reason": f"read_error_{type(exc).__name__}",
                })

    for hit_entry in list(results):
        if read_attempts >= max_dereferences:
            break

        content = hit_entry.get("content_snippet") or ""
        ref_stems = _extract_references_from_text(
            content,
            exclude_stems=seen_stems,
            limit=2,
        )

        for ref_stem in ref_stems:
            if read_attempts >= max_dereferences:
                break

            ref_path = f"Memory/notes/{ref_stem}.md"
            if ref_path in seen_paths or ref_stem in seen_stems or ref_path in attempted_paths:
                continue

            attempted_paths.add(ref_path)
            read_attempts += 1
            try:
                source = read_fn(ref_path)
                ok, digest, err = _validate_and_hash_source(source, ref_path)
                if ok and digest:
                    deref_entry = {
                        "retrieval_method": "dereferenced_from_pointer",
                        "read_path": ref_path,
                        "title": ref_stem,
                        "pointer_origin_path": hit_entry.get("read_path"),
                        "revision": digest,
                        "text": source.get("text"),
                        "source_provenance": "pointer_dereference_read",
                        "status": "resolved_pointer_target",
                    }
                    results.append(deref_entry)
                    seen_stems.add(ref_stem)
                    seen_paths.add(ref_path)
                    dereferenced_count += 1
                else:
                    warnings.append({
                        "type": "pointer_read_invalid",
                        "path": ref_path,
                        "pointer": hit_entry.get("read_path"),
                        "reason": err or "validation_failed",
                    })
            except Exception as exc:
                warnings.append({
                    "type": "pointer_read_error",
                    "path": ref_path,
                    "pointer": hit_entry.get("read_path"),
                    "reason": f"read_error_{type(exc).__name__}",
                })

    if read_attempts == 0:
        notice = "No full reads were performed (budget=0). Results contain unverified index snippets only."
    else:
        notice = (
            "Direct and referenced notes use the supplied read function. A mention in "
            "an index snippet does not prove relevance, a correction relationship, or "
            "current status. Full-read the referring source and check corrections. "
            "Read and search calls may have server-side recovery effects."
        )

    return {
        "query": cleaned_query,
        "results": results,
        "total_hits": len(results),
        "exact_stem_resolved": exact_stem_resolved,
        "dereferenced_count": dereferenced_count,
        "read_attempts": read_attempts,
        "warnings": warnings,
        "index_coverage": "unknown",
        "notice": notice,
    }
