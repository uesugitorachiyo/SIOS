"""Coordinate the protected PostgreSQL role and service transitions."""

from contextlib import contextmanager
import fcntl
import os
import stat

from .protected_writer_transition import WriterTransition


class FenceFailure(RuntimeError):
    pass


class WriterSupervisor:
    """Only the trusted host launcher may construct this coordinator.

    The bound PostgreSQL connection factory uses the administrator's private
    socket. The service controller owns the two manually started writer jobs.
    The cutover verifier checks the sealed source and protected handoff.
    """

    def __init__(self, *, connect, cluster, services, verify_cutover, lock):
        self.connect = connect
        self.cluster = cluster
        self.services = services
        self.verify_cutover = verify_cutover
        self.lock = lock

    def _stop_writers(self):
        failures = []
        for mode in ('application', 'recovery'):
            try:
                self.services.stop_writer(mode)
            except Exception as error:
                failures.append(error)
        if failures:
            raise FenceFailure('writer service stop failed') from failures[0]
        self.services.prove_writers_stopped()

    def _disable_logins(self):
        with self.connect() as db:
            if WriterTransition(db, self.cluster).close() != 'closed':
                raise FenceFailure('PostgreSQL writer closure did not verify')
            timestamp = db.scalar('SELECT pg_catalog.pg_postmaster_start_time()::text')
            if not timestamp:
                raise FenceFailure('could not read closed PostgreSQL start time')
            return timestamp

    def _seal(self):
        try:
            self._stop_writers()
            return self._disable_logins()
        except Exception as error:
            try:
                self.services.stop_database()
            except Exception as stop_error:
                raise FenceFailure('writer closure and emergency database stop both require inspection') from stop_error
            raise FenceFailure('writer closure failed and the protected PostgreSQL job was stopped') from error

    def _activate(self, mode, prepare=None):
        self.verify_cutover()
        timestamp = self._seal()
        try:
            self.services.restart_database()
            self.services.prove_writers_stopped()
            self.verify_cutover()
            if prepare is not None:
                prepare()
            with self.connect() as db:
                if WriterTransition(db, self.cluster).open_after_restart(mode, timestamp) != mode:
                    raise FenceFailure('PostgreSQL writer admission did not verify')
            self.services.start_writer(mode)
            self.services.prove_only_writer(mode)
        except BaseException:
            self._seal()
            raise

    def close(self):
        with self.lock():
            self._seal()

    def start_application(self):
        """Open ordinary Hindsight through an explicit operator action."""
        with self.lock():
            self._activate('application')

    @contextmanager
    def recovery(self, prepare=None):
        """Hold the exclusive transition lock from closure through recovery shutoff."""
        with self.lock():
            self._activate('recovery', prepare)
            active = True

            def prove_live():
                if not active:
                    return False
                try:
                    self.services.prove_only_writer('recovery')
                    self.verify_cutover()
                    with self.connect() as db:
                        WriterTransition(db, self.cluster).prove_recovery()
                except Exception:
                    return False
                return True

            try:
                yield prove_live
            finally:
                active = False
                self._seal()


@contextmanager
def administrator_lock(path):
    """Lock an installer-created file inside a private administrator directory."""
    if os.geteuid() != 0:
        raise PermissionError('administrator identity required')
    parent = path.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0
            or stat.S_IMODE(parent.st_mode) != 0o700):
        raise PermissionError('unsafe administrator directory')
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise PermissionError('unsafe administrator lock')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FenceFailure('another writer operation is active') from None
        yield
    finally:
        os.close(descriptor)
