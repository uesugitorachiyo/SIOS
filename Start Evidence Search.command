#!/bin/bash
set -u
cd -- "$(dirname -- "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  printf '%s\n' 'Setup is required. See docs/EVIDENCE_SEARCH.md in this folder.'
  read -r -p 'Press Enter to close.'
  exit 1
fi
PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}" .venv/bin/python -m aios.evidence_search_web --registered sios-l0 --open
result=$?
if [ "$result" -ne 0 ]; then
  printf '%s\n' 'Evidence Search could not start. See docs/EVIDENCE_SEARCH.md.'
  read -r -p 'Press Enter to close.'
fi
exit "$result"
