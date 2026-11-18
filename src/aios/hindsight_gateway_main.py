"""Explicit, foreground-only assembly of the experimental Hindsight gateway."""
from __future__ import annotations
import argparse
import os
from pathlib import Path

from .hindsight_backend import HindsightBackend
from .hindsight_gateway import serve
from .hindsight_journal import PrimaryJournal
from .hindsight_local_client import AuthenticatedLocalHindsight


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--gateway-config', type=Path, required=True)
    parser.add_argument('--hindsight-endpoint', required=True)
    parser.add_argument('--hindsight-ca', type=Path, required=True)
    parser.add_argument('--bank', required=True)
    parser.add_argument('--journal-root', type=Path, required=True)
    parser.add_argument('--target-lock', type=Path, required=True)
    parser.add_argument('--project-tag', action='append', required=True)
    args = parser.parse_args(argv)
    token = os.environ.get('SIOS_HINDSIGHT_TOKEN', '')
    try:
        client = AuthenticatedLocalHindsight(args.hindsight_endpoint, args.bank, token=token, ca_file=args.hindsight_ca)
        backend = HindsightBackend(client, args.journal_root, args.target_lock, args.project_tag)
        with PrimaryJournal(args.journal_root, target_lock=args.target_lock):
            pass
        serve(args.gateway_config, backend)
    except (ValueError, OSError) as exc:
        # Configuration, socket and filesystem exceptions can include private values.
        print('gateway_start_or_runtime_failed', file=__import__('sys').stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
