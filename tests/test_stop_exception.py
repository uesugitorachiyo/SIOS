import contextlib
import io
import threading
import unittest
from aios.graceful_drain import serve_draining


class Server:
    def __init__(self):
        self.exited = threading.Event()
    def process_request(self, request, address):
        pass
    def process_request_thread(self, request, address):
        pass
    def serve_forever(self):
        self.exited.wait()
    def shutdown(self):
        self.exited.set()
    def server_close(self):
        pass


class TestStopException(unittest.TestCase):
    def test_exception_never_retries_without_timeout(self):
        server = Server()
        calls = []
        class Worker:
            thread = threading.Thread(target=lambda: None)
            def stop(self, timeout=None):
                calls.append(timeout)
                raise RuntimeError('fixture')
        worker = Worker()
        errors = io.StringIO()
        def ready():
            raise KeyboardInterrupt()
        # Force a shutdown through the same finally path as an early failure.
        # on_ready exceptions precede the signal loop, so use a separate test
        # server that exits and triggers the loop's failure path instead.
        server.exited.set()
        with contextlib.redirect_stderr(errors):
            with self.assertRaisesRegex(RuntimeError, 'http_accept_stopped'):
                serve_draining(server, worker, retry_timeout=0.03)
        self.assertEqual(calls, [0.03])
        self.assertIn('sios_retry_worker_stop_unconfirmed', errors.getvalue())
