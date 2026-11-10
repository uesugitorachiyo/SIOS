"""Fictional transport tests; never contacts a real SIOS endpoint."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'src' / 'aios' / 'client_ready_proof.py'
spec = importlib.util.spec_from_file_location('fictional_ready_proof', MODULE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
CaptureReadiness = module.CaptureReadiness

STEM = 'selective-' + 'a' * 64
PATH = 'Memory/notes/' + STEM + '.md'
TEXT = '架空の記録 café\n'
REV = hashlib.sha256(TEXT.encode()).hexdigest()

def receipt():
    return {'status': 'captured', 'path': PATH, 'revision': REV,
            'receipt': {'request_id': 'fake', 'result': {'path': PATH, 'revision': REV}},
            'index_refresh': {'ready': True, 'reason': None}}

def status(checksum=REV):
    return {'total_files': 1, 'observed_files': [{'path': 'notes/' + STEM + '.md', 'checksum': checksum}]}

def search():
    return {'results': [{'title': STEM, 'type': 'entity', 'file_path': 'notes/' + STEM + '.md', 'read_path': PATH}],
            'total': 1, 'has_more': False, 'current_page': 1, 'page_size': 10, 'total_is_exact': True}

def source():
    return {'path': PATH, 'revision': REV, 'text': TEXT}

class Probe:
    def __init__(self):
        self.calls = []
        self.responses = {'health': {'ok': True, 'database': 'ok'}, 'memory_index_status': status(),
                          'memory_index_search': search(), 'read': source()}
    def __call__(self, operation, params):
        self.calls.append((operation, params))
        value = self.responses[operation]
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            return value()
        return copy.deepcopy(value)

class Tests(unittest.TestCase):
    def test_receipt_returns_immediately_and_exact_observation_is_separate(self):
        probe = Probe(); before = receipt(); original = copy.deepcopy(before); gate = CaptureReadiness(probe)
        got = gate.review(before)
        self.assertEqual(got['index_refresh'], {'ready': False, 'reason': 'search_visibility_unverified', 'server_reported_ready': True})
        self.assertFalse(probe.calls)
        self.assertEqual(got['receipt'], original['receipt'])
        self.assertEqual(before, original)
        self.assertTrue(gate.observe(PATH, REV)['ready'])
        self.assertEqual([x[0] for x in probe.calls], ['health', 'memory_index_status', 'memory_index_search', 'read', 'memory_index_status'])
        self.assertEqual(probe.calls[2][1], {'query': STEM})

    def test_missing_target_or_pointer_only_never_counts(self):
        for rows in ([], [{'title': 'pointer', 'type': 'entity', 'file_path': 'notes/pointer.md', 'read_path': 'Memory/notes/pointer.md'}]):
            probe = Probe(); probe.responses['memory_index_search']['results'] = rows
            result = CaptureReadiness(probe).observe(PATH, REV)
            self.assertEqual(result['reason'], 'not_confirmed')
            self.assertFalse(result['ready'])

    def test_stale_or_incomplete_status_fails_closed(self):
        for value in (status('0' * 64), {'total_files': 2, 'observed_files': status()['observed_files']},
                      {'total_files': 2, 'observed_files': status()['observed_files'] * 2},
                      {'total_files': True, 'observed_files': status()['observed_files']}):
            probe = Probe(); probe.responses['memory_index_status'] = value
            self.assertFalse(CaptureReadiness(probe).observe(PATH, REV)['ready'])

    def test_search_pagination_or_count_contradictions_are_unconfirmed(self):
        for change in ({'total': 0}, {'has_more': 'false'}, {'current_page': 2}, {'page_size': 0},
                       {'total_is_exact': 'not_boolean', 'has_more': True, 'total': 1},
                       {'total_is_exact': False}, {'total_is_exact': 1}, {'total': True},
                       {'has_more': True}, {'total': 2, 'has_more': False},
                       {'total': 2, 'has_more': True}, {'page_size': True}, {'current_page': True}):
            probe = Probe(); probe.responses['memory_index_search'].update(change)
            self.assertFalse(CaptureReadiness(probe).observe(PATH, REV)['ready'])
        for missing in ('total', 'has_more', 'current_page', 'page_size', 'total_is_exact'):
            probe = Probe(); del probe.responses['memory_index_search'][missing]
            self.assertFalse(CaptureReadiness(probe).observe(PATH, REV)['ready'])

    def test_valid_full_first_page_with_more_results(self):
        probe = Probe(); probe.responses['memory_index_search'].update(total=2, has_more=True, page_size=1)
        self.assertTrue(CaptureReadiness(probe).observe(PATH, REV)['ready'])

    def test_source_hash_path_and_final_status_must_match(self):
        for override in ({'path': PATH, 'revision': REV, 'text': 'wrong'},
                         {'path': 'Memory/notes/other.md', 'revision': REV, 'text': TEXT},
                         {'path': PATH, 'revision': '0' * 64, 'text': TEXT}):
            probe = Probe(); probe.responses['read'] = override
            self.assertFalse(CaptureReadiness(probe).observe(PATH, REV)['ready'])
        probe = Probe(); count = [0]
        def status_changed():
            count[0] += 1
            return status() if count[0] == 1 else status('0' * 64)
        probe.responses['memory_index_status'] = status_changed
        self.assertFalse(CaptureReadiness(probe).observe(PATH, REV)['ready'])

    def test_identity_read_failure_and_oversize_are_unconfirmed(self):
        for operation, value in [('health', {'ok': False}), ('health', {'ok': True, 'database': 'corrupt'}),
                                 ('health', {'ok': True}), ('health', {'ok': True, 'database': 1}),
                                 ('read', RuntimeError('fake sensitive output')),
                                 ('memory_index_status', {'total_files': 1, 'observed_files': status()['observed_files'], 'padding': 'x' * 3000})]:
            probe = Probe(); probe.responses[operation] = value
            result = CaptureReadiness(probe, max_status_bytes=1024).observe(PATH, REV)
            self.assertFalse(result['ready'])
            self.assertNotIn('sensitive', json.dumps(result))

    def test_non_selective_or_unchanged_unconfirmed_receipts(self):
        probe = Probe(); x = receipt(); x['path'] = 'Memory/notes/nonselective.md'
        gate = CaptureReadiness(probe)
        self.assertFalse(gate.review(x)['index_refresh']['ready'])
        self.assertFalse(gate.observe(x['path'], REV)['ready'])
        self.assertFalse(probe.calls)
        x = receipt(); x['index_refresh'] = {'ready': False, 'reason': 'refresh_unavailable'}
        self.assertIs(CaptureReadiness(probe).review(x), x)
        self.assertFalse(probe.calls)
        skip = {'status': 'skipped'}
        self.assertIs(CaptureReadiness(probe).review(skip), skip)
        anomalous = receipt(); anomalous['status'] = 'unexpected'
        self.assertFalse(gate.review(anomalous)['index_refresh']['ready'])
        self.assertTrue(gate.review(anomalous)['index_refresh']['server_reported_ready'])
        self.assertFalse(probe.calls)

    def test_timeout_and_concurrent_capture_never_resend_or_spawn_more_probes(self):
        entered = threading.Event(); release = threading.Event(); done = threading.Event(); calls = []
        def stuck(operation, params):
            calls.append(operation); entered.set(); release.wait(3); done.set(); return {'ok': True}
        gate = CaptureReadiness(stuck, timeout=.06)
        receipt_one = gate.review(receipt()); receipt_two = gate.review(receipt())
        self.assertEqual(receipt_one['receipt'], receipt()['receipt']); self.assertEqual(receipt_two['receipt'], receipt()['receipt'])
        before = time.monotonic(); first = gate.observe(PATH, REV); elapsed = time.monotonic() - before
        self.assertTrue(entered.is_set()); self.assertLess(elapsed, .8)
        self.assertFalse(first['ready'])
        second = gate.observe(PATH, REV)
        self.assertFalse(second['ready']); self.assertEqual(calls, ['health'])
        release.set(); self.assertTrue(done.wait(2))

    def test_late_success_cannot_turn_timed_out_result_true(self):
        probe = Probe(); release = threading.Event()
        def slow_health():
            release.wait(.3); return {'ok': True}
        probe.responses['health'] = slow_health
        result = CaptureReadiness(probe, timeout=.04).observe(PATH, REV)
        self.assertFalse(result['ready'])
        release.set(); time.sleep(.1)
        self.assertFalse(result['ready'])
        self.assertEqual([x[0] for x in probe.calls], ['health'])

    def test_dangling_observation_does_not_keep_process_alive(self):
        script = '''import importlib.util,sys,time\ns=importlib.util.spec_from_file_location('p',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\np='Memory/notes/selective-'+('a'*64)+'.md'\ndef slow(*a):time.sleep(20)\nx=m.CaptureReadiness(slow,timeout=.05).observe(p,'b'*64);print(x['ready'],flush=True)\n'''
        proc = subprocess.run([sys.executable, '-B', '-c', script, str(MODULE)], capture_output=True, text=True, timeout=3)
        self.assertEqual(proc.returncode, 0); self.assertEqual(proc.stdout.strip(), 'False')

if __name__ == '__main__':
    unittest.main()
