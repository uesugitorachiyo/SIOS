# Radar source-outcome preflight

`aios.radar_coverage` validates a supplied research record before an ordinary
Opportunity Radar report is published. It distinguishes an unchanged duplicate,
a changed fact, a checked source with no qualifying change, an old or undated
fact, an unavailable source, and a verification conflict. It does not fetch
sources, judge business relevance, publish a report, or prove a scheduled run.

Keep real per-run JSON, drafts, and reports in a private workspace outside this
repository. From the source checkout, run:

```sh
PYTHONPATH=src python3 -m aios.radar_coverage /private/run-sources.json
PYTHONPATH=src python3 -m aios.radar_coverage /private/run-sources.json --report /private/draft.md
```

The first command generates the exact `## Source outcomes` section. Put it in
the draft and run the second command before the existing publish operation.
Pass the record's `status` and `items` unchanged to publish. An unavailable or
conflicting source requires `partial`; a completed report requires declared
adequate coverage. The operator still verifies source facts and decides whether
coverage and relevance are adequate. Existing reports need no conversion.

Minimal **synthetic** record:

```json
{
  "routine": "opportunity-radar",
  "run_id": "fixture-not-published",
  "status": "partial",
  "coverage_assessment": "limited",
  "items": [],
  "sources": [{
    "url": "https://example.org/projects",
    "accessed": "2026-10-21",
    "outcome": "unavailable",
    "note": "Public details could not be verified"
  }]
}
```

This is a reporting consistency check, not an automated source classifier. A
missing run record is not a zero-signal result.
