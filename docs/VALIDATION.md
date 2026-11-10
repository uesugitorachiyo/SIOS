# Public source validation scope

The developer uses a private local SIOS instance in ongoing work. That use and
the public-source checks below are different evidence: the private instance
has its own configuration, data and integrations, and is not reproduced by
the public test fixtures. Neither establishes general production readiness.

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
