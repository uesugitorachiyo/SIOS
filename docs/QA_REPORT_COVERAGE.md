# Offline QA report coverage

Run this existing utility after a fixed candidate and an independent review
report exist. It checks declared requirements against supplied local evidence;
it never invokes a model, runs project tests, changes a service or approves
deployment. All inputs are user-owned files outside the source checkout.

```sh
python -m aios.qa_coverage request.json report.json --evidence-root artifacts
```

The request identifies `base`, `candidate`, `specification`, `diff` and `role`
as objects containing a relative `path` and lowercase SHA-256 `sha256`. It
also requires `developer_id`, nonempty `scope` and `rationale`, `exclusions`
and `known_limitations` lists, and 1–512 `requirements`. Each requirement has
a unique `id`, category `QA-01` through `QA-06`, and exact `text`. Declare
coverage of all six categories explicitly; the checker does not invent the
meaning of a category or decide that the requirements are sufficient.

The report binds the five artifact hashes as `<name>_sha256`, declares
`reviewer_id`, `review_environment`, and `modes` chosen from `code_review`,
`architecture_review`, and `refactor_assessment`. Include a `checks` entry
per requirement and a `findings` list. Each check names `requirement_id`,
`status` (`PASS`, `FAIL`, `ERROR`, `UNKNOWN`, `NOT_RUN`), and `evidence`
descriptors. Every non-PASS needs a reason. A FAIL also needs a finding with
`id`, `requirement_id`, `location`, `expected`, `actual`, `reproduction`,
`impact`, `owner`, `disposition`, and optional `related_security_id`.

`report_consistency=PASS` means the fixed artifacts, coverage and evidence
hashes are internally consistent. `reported_requirement_outcome=FAIL`
preserves a failing requirement even if the report structure is valid.
Missing evidence remains UNKNOWN; mismatched bytes, unsafe paths, duplicate
checks and unsupported PASS claims fail. The JSON classification, rather
than the process exit code alone, determines the report outcome.

The output always includes `approval_to_deploy=NOT_GRANTED`,
`independent_review_attested=false` and `security_clearance_attested=false`.
Self-declared reviewer names do not authenticate independence. This offline
checker does not establish temporal freshness, test isolation, code quality,
Mission completion or a complete QA workflow. Synthetic examples and
regression cases are in `tests/test_qa_coverage.py`; no real review records
are shipped.
