import subprocess
import unittest
from unittest.mock import patch

from aios.macos_hindsight.verify_sealed_handoff import verify_retired_routes
from aios.macos_hindsight.protected_host_binding import JOB_IDS, validate_deployment


class RetiredRouteTests(unittest.TestCase):
    def test_each_configured_service_and_port_must_be_absent(self):
        labels = ['example.database', 'example.gateway']
        ports = [15432, 18080]
        targets = [f'{domain}/501/{label}' for domain in ('gui', 'user') for label in labels]
        targets += [f'-iTCP:{port}' for port in ports]
        for present in targets:
            def probe(command):
                if command[0] == '/bin/launchctl':
                    return subprocess.CompletedProcess(command, 0 if command[2] == present else 113,
                                                       '', '' if command[2] == present else 'Could not find service')
                return subprocess.CompletedProcess(command, 0 if command[3] == present else 1, '', '')
            with self.subTest(route=present), patch('aios.macos_hindsight.verify_sealed_handoff._probe', probe):
                with self.assertRaises(PermissionError):
                    verify_retired_routes(501, labels, ports)

    def test_missing_or_invalid_inventory_is_rejected(self):
        for labels, ports in (([], [15432]), (['valid'], []), (['bad/name'], [15432]), (['ok'], [0])):
            with self.subTest(labels=labels, ports=ports), self.assertRaises(ValueError):
                verify_retired_routes(501, labels, ports)


class AdministratorEntryTests(unittest.TestCase):
    def test_unprivileged_process_cannot_start_administrator_mcp(self):
        import os
        import sys
        if os.getuid() == 0:
            self.skipTest('requires an unprivileged process')
        process = subprocess.run([sys.executable, '-m', 'aios.macos_hindsight.protected_recovery_stdio'],
                                 input='', capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(process.stdout, '')
        self.assertIn('administrator_recovery_access_denied', process.stderr)

    def test_unprivileged_process_cannot_resume_writer(self):
        import os
        import sys
        if os.getuid() == 0:
            self.skipTest('requires an unprivileged process')
        process = subprocess.run([sys.executable, '-m', 'aios.macos_hindsight.resume_application'],
                                 input='', capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(process.stdout, '')
        self.assertIn('administrator_writer_access_denied', process.stderr)

    def test_administrator_resume_checks_single_writer(self):
        from aios.macos_hindsight.resume_application import main
        with patch('aios.macos_hindsight.resume_application.os.getuid', return_value=0), \
             patch('aios.macos_hindsight.resume_application.os.geteuid', return_value=0), \
             patch('aios.macos_hindsight.resume_application.os.chdir'), \
             patch('aios.macos_hindsight.protected_host_binding.bind') as bind:
            self.assertEqual(main(), 0)
            bind.return_value.start_application.assert_called_once_with()
            bind.return_value.services.prove_only_writer.assert_called_once_with('application')
            bind.return_value.start_application.side_effect = RuntimeError('fixture failure')
            self.assertEqual(main(), 1)
            bind.return_value.close.assert_called_once_with()

    def test_protected_configuration_requires_complete_disjoint_inventory(self):
        config = {
            'legacy_uid': 501, 'port': 15433, 'source_id': '111', 'target_id': '222',
            'receipt_sha256': 'a' * 64, 'original_pgdata': '/private/fixture/database',
            'original_inode': [1, 2], 'original_journals': '/private/fixture/journals',
            'original_journal_inode': [1, 3], 'jobs': {key: 'b' * 64 for key in JOB_IDS},
            'legacy_labels': ['example.old.database'], 'legacy_ports': [15432], 'legacy_socket_port': 15432,
        }
        self.assertEqual(validate_deployment(config), config)
        mutations = [
            {'legacy_labels': []}, {'legacy_ports': []}, {'legacy_socket_port': 15431},
            {'legacy_ports': [15432, 15433]}, {'legacy_labels': [JOB_IDS['recovery'][0]]},
            {'source_id': '222'}, {'original_pgdata': '../relative'}, {'original_inode': [True, 2]},
        ]
        for change in mutations:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_deployment(dict(config, **change))


if __name__ == '__main__':
    unittest.main()
