# Offline Mission evidence consistency check

`aios.qa_evidence` checks a supplied export of Mission catalog and full-read
results. It does not contact a server, authenticate an export, approve a
release or attest that any current writer is blocked.

Independently select the Mission slugs to check. An authorized client exports
one `catalog(store="missions")` result and one full
`read(relative="Missions/<slug>/mission.md")` result per selected Mission.
Record the instance identifier and timezone-aware observation time for each
request. Keep the complete returned text in a local JSON bundle outside this
repository:

```json
{
  "instance": "verified-instance-id",
  "catalog": {
    "store": "missions",
    "instance": "verified-instance-id",
    "observed_at": "2026-01-01T18:00:00Z",
    "paths": ["Missions/fixture-one/mission.md"]
  },
  "reads": [{
    "instance": "verified-instance-id",
    "observed_at": "2026-01-01T18:00:00Z",
    "path": "Missions/fixture-one/mission.md",
    "revision": "<64-character SHA-256>",
    "text": "<complete returned text>"
  }],
  "receipts": [{"path": "relative/receipt.md", "sha256": "<64-character SHA-256>"}]
}
```

Use existing receipt files below a separate declared receipt root. Run the
check with one `--mission` argument per independently selected slug:

```sh
PYTHONPATH=src python3 -m aios.qa_evidence /private/bundle.json \
  --receipt-root /private/receipts --mission fixture-one
```

The output contains statuses and paths, not Mission text. `PASS` means the
supplied bundle is internally consistent: a fresh catalog contains every
specified path, each same-instance full read has a matching revision, and
the supplied receipt hashes match. Missing, stale, future-dated or
inaccessible evidence is `UNKNOWN`; a content/hash mismatch or unsafe
receipt path is `FAIL`. `current_operational_status` remains `UNKNOWN`
because an offline export cannot prove the current server or permissions.

Record requirement, build and configuration identity, source revisions, test
date, environment and result in a separate private human review. A receipt
for another build or client does not qualify the current one.
