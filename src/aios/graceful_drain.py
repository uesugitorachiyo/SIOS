"""Candidate SIGTERM drain; unfinished work is reported, not called complete."""
import signal
import sys
import threading
import time
import types


def serve_draining(server, retry_worker, *, drain_timeout=3.0, retry_timeout=1.0, report_after=None, on_ready=None):
    if report_after is not None:
        drain_timeout = min(drain_timeout, float(report_after))
    closing = threading.Event()
    changed = threading.Condition()
    active = 0
    process = server.process_request
    process_thread = server.process_request_thread

    def register(self, request, address):
        nonlocal active
        with changed:
            if closing.is_set():
                self.shutdown_request(request)
                return
            active += 1
        try:
            process(request, address)
        except BaseException:
            with changed:
                active -= 1
                changed.notify_all()
            raise

    def handle(self, request, address):
        nonlocal active
        try:
            process_thread(request, address)
        finally:
            with changed:
                active -= 1
                changed.notify_all()

    server.process_request = types.MethodType(register, server)
    server.process_request_thread = types.MethodType(handle, server)
    old_term = None
    if threading.current_thread() is threading.main_thread():
        try:
            old_term = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, lambda *_: closing.set())
        except (ValueError, OSError):
            old_term = None
    thread = threading.Thread(target=server.serve_forever, name='sios-http-accept')
    thread.start()
    try:
        try:
            if on_ready is not None:
                on_ready()
            try:
                while not closing.wait(.2):
                    if not thread.is_alive():
                        raise RuntimeError('http_accept_stopped')
            except KeyboardInterrupt:
                closing.set()
        finally:
            closing.set()
            if thread.is_alive():
                server.shutdown()
            server.server_close()
            with changed:
                deadline = time.monotonic() + drain_timeout
                while active:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        print('sios_drain_wait_timeout', file=sys.stderr, flush=True)
                        break
                    changed.wait(remaining)
            try:
                retry_worker.stop(timeout=retry_timeout)
            except Exception:
                print('sios_retry_worker_stop_unconfirmed', file=sys.stderr, flush=True)
            if retry_worker.thread.is_alive():
                print('sios_retry_worker_wait_timeout', file=sys.stderr, flush=True)
            thread.join(timeout=0.5)
    finally:
        if old_term is not None and threading.current_thread() is threading.main_thread():
            try:
                signal.signal(signal.SIGTERM, old_term)
            except (ValueError, OSError):
                pass
