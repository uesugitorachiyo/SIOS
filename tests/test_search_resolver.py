"""Comprehensive synthetic tests for bounded memory search resolver with full-source verification.

Zero production interaction; 100% in-memory synthetic fixture data.
Zero Linux/VM dependencies; native macOS Python.
"""
from __future__ import annotations

import hashlib
import json
import unittest

from aios.search_resolver import resolve_search_verified, resolve_search


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _format_note(stem: str, meaning_id: str, claim: str, conditions: list[str], supersedes: str | None = None) -> str:
    body = {
        "schema": "aios-selective-memory-v1",
        "meaning_id": meaning_id,
        "claim": claim,
        "conditions": conditions,
        "supersedes": supersedes,
        "evidence_status": "verified",
        "recorded_at": "2026-11-01T00:00:00Z",
    }
    json_str = json.dumps(body, indent=2)
    return (
        f"---\ntitle: {stem}\ntype: note\npermalink: aios/notes/{stem}\n---\n\n"
        f"<!-- aios-selective-memory-v1 -->\n# Selective Memory: {meaning_id}\n\n"
        f"```json\n{json_str}\n```\n\n{claim}\n"
    )


class TestSearchVerifiedResolver(unittest.TestCase):
    def setUp(self):
        self.store: dict[str, dict[str, str]] = {}
        self.fts: dict[str, list[dict]] = {}

    def _add_document(self, stem: str, meaning_id: str, claim: str, conditions: list[str], supersedes: str | None = None) -> tuple[str, str]:
        path = f"Memory/notes/{stem}.md"
        text = _format_note(stem, meaning_id, claim, conditions, supersedes)
        rev = _hash(text)
        self.store[path] = {"path": path, "revision": rev, "text": text}
        return path, rev

    def _add_fts_hit(self, query: str, stem: str, snippet: str, score: float = -10.0):
        path = f"Memory/notes/{stem}.md"
        hit = {
            "title": stem,
            "read_path": path,
            "score": score,
            "content": snippet,
        }
        self.fts.setdefault(query, []).append(hit)

    def _search_fn(self, q: str) -> dict:
        hits = self.fts.get(q, [])
        return {"results": hits, "has_more": False, "total": len(hits)}

    def _read_fn(self, path: str) -> dict:
        if path not in self.store:
            raise FileNotFoundError(f"not found: {path}")
        return dict(self.store[path])

    def test_1_snippets_not_trusted_without_referring_full_read(self):
        """Snippet says it references a target, but referring note full text does NOT."""
        referring_stem = "selective-" + "a" * 64
        fake_target_stem = "selective-" + "f" * 64

        self._add_document(referring_stem, "actual-ref-meaning", "Actual full text without reference.", ["cond1"])

        fake_snippet = f"Snippet erroneously claiming reference to Memory/notes/{fake_target_stem}.md"
        self._add_fts_hit("query", referring_stem, fake_snippet)

        calls = []
        def tracked_read(path):
            calls.append(path)
            return self._read_fn(path)

        res = resolve_search_verified("query", self._search_fn, tracked_read, max_reads=5)
        self.assertIn(f"Memory/notes/{referring_stem}.md", calls)
        self.assertNotIn(f"Memory/notes/{fake_target_stem}.md", calls)
        self.assertEqual(len(res["results"]), 1)
        self.assertEqual(res["results"][0]["status"], "verified_referring_source")

    def test_2_verified_referring_note_and_verified_target_dereferenced(self):
        """When referring note's verified full text mentions target, target is full-read and verified."""
        target_stem = "selective-" + "1" * 64
        _, target_rev = self._add_document(target_stem, "original-meaning", "Original claim.", ["cond1"])

        referring_stem = "selective-" + "2" * 64
        claim = f"Pointer note mentioning Memory/notes/{target_stem}.md explicitly."
        self._add_document(referring_stem, "pointer-meaning", claim, ["cond2"])

        self._add_fts_hit("design", referring_stem, "Snippet for design.")

        res = resolve_search_verified("design", self._search_fn, self._read_fn, max_reads=5)
        self.assertEqual(res["total_results"], 2)
        self.assertEqual(res["referring_verified_count"], 1)
        self.assertEqual(res["targets_verified_count"], 1)

        ref_hit = res["results"][0]
        self.assertEqual(ref_hit["status"], "verified_referring_source")
        self.assertEqual(ref_hit["stem"], referring_stem)

        target_hit = res["results"][1]
        self.assertEqual(target_hit["status"], "resolved_target_original")
        self.assertEqual(target_hit["stem"], target_stem)
        self.assertEqual(target_hit["revision"], target_rev)
        self.assertEqual(target_hit["relationship_to_pointer"], "verified_mention_in_referring_source")

    def test_3_supersedes_relationship_confirmed_from_full_json(self):
        """When referring note declares supersedes: target_stem in full JSON, relationship is attributed."""
        old_stem = "selective-" + "3" * 64
        self._add_document(old_stem, "old-meaning", "Old claim.", ["cond1"])

        new_stem = "selective-" + "4" * 64
        claim = f"Correction superseding {old_stem}."
        self._add_document(new_stem, "new-meaning", claim, ["cond2"], supersedes=old_stem)

        self._add_fts_hit("correction", new_stem, "Snippet mentioning correction.")

        res = resolve_search_verified("correction", self._search_fn, self._read_fn, max_reads=5)
        self.assertEqual(res["total_results"], 2)

        target_hit = res["results"][1]
        self.assertEqual(target_hit["stem"], old_stem)
        self.assertEqual(target_hit["relationship_to_pointer"], "referring_source_supersedes_target")

    def test_4_invalid_search_response_format_raises_value_error(self):
        """Invalid search response formats are not masked as empty results."""
        with self.assertRaises(ValueError):
            resolve_search_verified("query", lambda q: "not a dict", self._read_fn)

        with self.assertRaises(ValueError):
            resolve_search_verified("query", lambda q: {"invalid": "payload"}, self._read_fn)

    def test_5_invalid_selective_schema_fails_closed(self):
        """Document missing aios-selective-memory-v1 schema or required fields is rejected."""
        stem = "selective-" + "8" * 64
        path = f"Memory/notes/{stem}.md"
        # Invalid body: missing required conditions and schema marker
        invalid_body = f"---\ntitle: {stem}\n---\n\nPlain text without schema."
        rev = _hash(invalid_body)
        self.store[path] = {"path": path, "revision": rev, "text": invalid_body}
        self.fts[stem] = []

        res = resolve_search_verified(stem, self._search_fn, self._read_fn, max_reads=3)
        self.assertFalse(res["exact_stem_resolved"])
        self.assertEqual(len(res["results"]), 0)
        self.assertTrue(any(w["type"] == "exact_stem_read_invalid" for w in res["warnings"]))

    def test_6_notice_is_truthful_when_budget_is_zero(self):
        """When max_reads=0, notice must state that no full reads were performed."""
        stem = "selective-" + "9" * 64
        self._add_document(stem, "test-meaning", "Some claim", ["cond"])
        self._add_fts_hit("query", stem, "Snippet")

        res = resolve_search_verified("query", self._search_fn, self._read_fn, max_reads=0)
        self.assertEqual(res["read_budget"]["read_attempts"], 0)
        self.assertIn("budget=0", res["notice"])
        self.assertIn("unverified index snippets only", res["notice"])
        self.assertEqual(res["results"][0]["status"], "unverified_indexed_hit")

    def test_7_backwards_compatibility_resolve_search_contract(self):
        """resolve_search maintains the exact keys and contract of reviewed-v2, and accepts non-selective docs."""
        target_stem = "selective-" + "b" * 64
        self._add_document(target_stem, "test-meaning", "Target body", ["cond"])
        self.fts[target_stem] = []

        res = resolve_search(target_stem, self._search_fn, self._read_fn, max_dereferences=2)
        # Check exact top-level keys
        expected_keys = {
            "query", "results", "total_hits", "exact_stem_resolved",
            "dereferenced_count", "read_attempts", "warnings",
            "index_coverage", "notice"
        }
        self.assertEqual(set(res.keys()), expected_keys)
        self.assertEqual(res["total_hits"], 1)
        self.assertEqual(res["results"][0]["read_path"], f"Memory/notes/{target_stem}.md")
        self.assertEqual(res["results"][0]["retrieval_method"], "direct_stem_read")

        # Also verify non-selective document with valid sha256 hash (review finding fix)
        plain_stem = "selective-" + "c" * 64
        plain_path = f"Memory/notes/{plain_stem}.md"
        plain_text = f"---\ntitle: {plain_stem}\n---\n\nPlain markdown without selective schema."
        plain_rev = _hash(plain_text)
        self.store[plain_path] = {"path": plain_path, "revision": plain_rev, "text": plain_text}
        self.fts[plain_stem] = []

        res_plain = resolve_search(plain_stem, self._search_fn, self._read_fn, max_dereferences=2)
        self.assertTrue(res_plain["exact_stem_resolved"])
        self.assertEqual(res_plain["total_hits"], 1)
        self.assertEqual(res_plain["results"][0]["read_path"], plain_path)
        self.assertEqual(res_plain["results"][0]["status"], "resolved_exact_source")


if __name__ == "__main__":
    unittest.main()
