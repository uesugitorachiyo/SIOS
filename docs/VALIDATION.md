# Public source validation scope

The developer uses a private local SIOS instance in ongoing work. That use and
the public-source checks below are different evidence: the private instance
has its own configuration, data and integrations, and is not reproduced by
the public test fixtures. Neither establishes general production readiness.

## Basic Memory 1.0 and Windows satellite integration (2026-11-09)

- On macOS, four fictional-data tests passed with the real Basic Memory 1.0.0
  launcher, including index creation, reindexing, capture and lookup.
- On a Windows host, 35 fictional-data tests passed for MCP client behavior,
  scoped search helpers and satellite launchers. These tests do not establish
  live Windows-to-Mac connectivity or qualify the optional 1.0 indexer on Windows.
- The macOS full suite ran 356 tests with six failures, 61 errors and four
  skips. All failure/error labels were in the historical Factory test suite;
  the pre-integration main baseline had the same 67 labels. This comparison
  does not establish their causes or a full-suite pass.

## Earlier alpha publication checks

The following historical checks apply to the 0.1.0a8 alpha source distribution,
not automatically to later changes. They covered a fresh Python 3.12
environment on macOS, not all client/operating-system combinations.

- Wheel and source archive built with the declared setuptools backend.
- Fresh wheel installation with agent, backup and verification extras.
- 129 synthetic unittest cases passed, including the actual installed wheel,
  fake clients, offline evidence, guarded writes and isolated recovery fixtures.
- CLI help, new standalone setup and health passed using a new private
  configuration and new data/cache roots. Brain, Memory and Missions began
  empty, with no seeded Markdown or imported history.
- Public-file boundary and credential scans passed for proposed files and
  the complete source Git history. Build contents were checked for unwanted
  instance data and developer paths.

Reproduce the source suite after installing the relevant dependencies:

```sh
python -B -m unittest discover -s tests -t . -v
python -B scripts/check_public_repo.py
```

The tests use synthetic fixtures. No real account, conversation, private
Mission, live credential or operational receipt is a public test dependency.
These checks do not attest every remote client, index-server integration,
live owner approval, protected completion or overall security. Automatic
scans cannot prove that all sensitive information or vulnerabilities are
absent. The proposed Software Factory coding/review helper remains unbuilt.
