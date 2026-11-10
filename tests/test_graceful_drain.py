"""Unit and integration tests for server graceful drain on SIGTERM / SIGINT."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import Request, urlopen

from aios.graceful_drain import serve_draining
from aios.index_retry import RetryWorker
from aios.server import Device, make_server
from aios.store import Store


class DummyRetryWorker:
    def __init__(self):
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name='dummy-retry', daemon=True)
        self.thread.start()

    def _run(self):
        self.stopped.wait()

    def stop(self, timeout=2.0):
        self.stopped.set()
        self.thread.join(timeout=timeout)


class GracefulDrainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='sios_drain_test_')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.store = Store(self.root)
        self.instance_id = "11111111-1111-4111-8111-111111111111"
        self.token = "test-token-" * 4
        self.device = Device("test-device", self.token, frozenset({"read", "memory:write"}))

    def _build_server(self, dispatcher=None):
        return make_server(
            self.store,
            self.instance_id,
            [self.device],
            bind="127.0.0.1",
            port=0,
            dispatcher=dispatcher,
        )

    @unittest.skipIf(os.name == 'nt', 'POSIX SIGTERM delivery test')
    def test_subprocess_sigterm_drains_in_flight_request(self):
        """Verify that an actual SIGTERM sent to a child process drains in-flight work."""
        worker_script = f"""import json, os, signal, sys, tempfile, threading, time
from pathlib import Path
from urllib.request import Request, urlopen
from aios.graceful_drain import serve_draining
from aios.server import Device, make_server
from aios.store import Store

root = Path(sys.argv[1]).resolve()
store = Store(root)
instance_id = "{self.instance_id}"
token = "{self.token}"

in_flight_entered = root / "in_flight_entered"
in_flight_release = root / "in_flight_release"

def slow_dispatcher(store, op, params, *, actor, scopes):
    in_flight_entered.touch()
    while not in_flight_release.exists():
        time.sleep(0.05)
    return {{"status": "ok"}}

device = Device("test-device", token, frozenset({{"read", "memory:write"}}))
server = make_server(store, instance_id, [device], bind="127.0.0.1", port=0, dispatcher=slow_dispatcher)
port = server.server_address[1]

class Worker:
    def __init__(self):
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.stopped.wait, daemon=True)
        self.thread.start()
    def stop(self, timeout=1.0):
        self.stopped.set()
        self.thread.join(timeout=timeout)

def ready():
    (root / "port").write_text(str(port))

retry = Worker()
serve_draining(server, retry, drain_timeout=3.0, retry_timeout=1.0, on_ready=ready)
(root / "clean_exit").touch()
"""
        proc = subprocess.Popen(
            [sys.executable, "-c", worker_script, str(self.root)],
            env=os.environ.copy(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            port_file = self.root / "port"
            for _ in range(50):
                if port_file.exists():
                    break
                time.sleep(0.1)
            self.assertTrue(port_file.exists())
            port = int(port_file.read_text().strip())

            client_result = []

            def call():
                body = json.dumps({"instance_id": self.instance_id, "operation": "read", "params": {}}).encode()
                req = Request(
                    f"http://127.0.0.1:{port}/v1/rpc",
                    body,
                    {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
                )
                try:
                    with urlopen(req, timeout=5.0) as resp:
                        client_result.append(json.load(resp))
                except Exception as e:
                    client_result.append(e)

            client_thread = threading.Thread(target=call)
            client_thread.start()

            in_flight_entered = self.root / "in_flight_entered"
            for _ in range(50):
                if in_flight_entered.exists():
                    break
                time.sleep(0.1)
            self.assertTrue(in_flight_entered.exists())

            proc.send_signal(signal.SIGTERM)

            time.sleep(0.2)
            new_failed = False
            try:
                body = json.dumps({"instance_id": self.instance_id, "operation": "read", "params": {}}).encode()
                req = Request(
                    f"http://127.0.0.1:{port}/v1/rpc",
                    body,
                    {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
                )
                with urlopen(req, timeout=1.0) as resp:
                    pass
            except (OSError, URLError):
                new_failed = True
            self.assertTrue(new_failed)

            (self.root / "in_flight_release").touch()
            client_thread.join(timeout=3.0)
            self.assertFalse(client_thread.is_alive())

            self.assertEqual(len(client_result), 1)
            self.assertIsInstance(client_result[0], dict)
            self.assertEqual(client_result[0].get("result"), {"status": "ok"})

            ret = proc.wait(timeout=5.0)
            self.assertEqual(ret, 0)
            self.assertTrue((self.root / "clean_exit").exists())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2.0)
            if proc.stderr:
                proc.stderr.close()

    def test_delayed_retry_worker_negative_control_and_bounded_shutdown(self):
        """Negative control: Verify real RetryWorker with delayed job respects bounded timeout."""
        # 1. Simulate an in-flight delayed job in RetryWorker
        job_running = threading.Event()
        release_job = threading.Event()

        def slow_retry_one(store, *, now=None, repair=None, stop_event=None):
            job_running.set()
            release_job.wait(timeout=6.0)
            return False

        # Create real RetryWorker
        worker = RetryWorker(self.store, poll_interval=1)
        with patch('aios.index_retry.retry_one', side_effect=slow_retry_one):
            worker.start()
            self.assertTrue(job_running.wait(timeout=3.0))

            # Old unpatched behavior: worker.stop() without timeout argument would wait 10s!
            # With fixed RetryWorker.stop(timeout=1.0):
            start = time.monotonic()
            worker.stop(timeout=1.0)
            elapsed = time.monotonic() - start

            # Must return within ~1.0-1.5s, well below 5.0s launchd limit!
            self.assertLess(elapsed, 2.0)
            # The background thread is still alive because slow_retry_one is sleeping
            self.assertTrue(worker.thread.is_alive())

            # Release job and clean up
            release_job.set()
            worker.thread.join(timeout=2.0)
            self.assertFalse(worker.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
