# Public source validation scope

Version 0.1.0a8 is an alpha source distribution. The bounded publication
checks cover a fresh Python 3.12 environment on macOS, not a production
deployment or all client/operating-system combinations.

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
