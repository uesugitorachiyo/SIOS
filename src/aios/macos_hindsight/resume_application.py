"""Reopen the ordinary protected writer after an administrator recovery."""

import os
import sys


def main():
    if os.getuid() != 0 or os.geteuid() != 0:
        print('administrator_writer_access_denied', file=sys.stderr)
        return 1
    supervisor = None
    try:
        os.chdir('/var/empty')
        from .protected_host_binding import bind

        supervisor = bind()
        supervisor.start_application()
        supervisor.services.prove_only_writer('application')
    except Exception:
        if supervisor is not None:
            try:
                supervisor.close()
            except Exception:
                print('protected_writer_shutdown_unverified', file=sys.stderr)
        print('protected_writer_resume_refused', file=sys.stderr)
        return 1
    print('protected_writer_started')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
