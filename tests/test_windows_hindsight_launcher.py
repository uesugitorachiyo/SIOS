"""Native Windows smoke test with fictional credentials and disposable files."""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import venv
from pathlib import Path


@unittest.skipUnless(os.name == 'nt' and shutil.which('openssl'), 'Windows and OpenSSL required')
class WindowsLauncherTests(unittest.TestCase):
    def test_dpapi_launcher_runs_installed_relay_and_checks_ca(self):
        from test_hindsight_relay import GatewayIntegrationTests
        GatewayIntegrationTests.setUpClass()
        try:
            with tempfile.TemporaryDirectory(prefix='sios launch ') as dirname:
                root = Path(dirname)
                venv.create(root / 'runtime', with_pip=False)
                python = root / 'runtime' / 'Scripts' / 'python.exe'
                package = root / 'runtime' / 'Lib' / 'site-packages' / 'aios'
                package.mkdir()
                (package / '__init__.py').write_text('')
                repo = Path(__file__).resolve().parents[1]
                shutil.copy2(repo / 'src' / 'aios' / 'hindsight_relay.py', package / 'hindsight_relay.py')
                shutil.copy2(GatewayIntegrationTests.ca, root / 'ca.crt')
                ca_hash = hashlib.sha256((root / 'ca.crt').read_bytes()).hexdigest()
                env = dict(os.environ, SIOS_TEST_ROOT=str(root))
                fake_enrollment = ("$s=ConvertTo-SecureString 'fixture-" + 'x' * 48 + "' -AsPlainText -Force; "
                                   "[IO.File]::WriteAllText((Join-Path $env:SIOS_TEST_ROOT 'gateway-token.dpapi'), "
                                   "(ConvertFrom-SecureString -SecureString $s), [Text.Encoding]::ASCII)")
                subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', fake_enrollment],
                               env=env, check=True, capture_output=True, timeout=30)
                cmd = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'RemoteSigned',
                       '-File', str(repo / 'templates' / 'windows-hindsight-relay.ps1'), '-SetupRoot', str(root),
                       '-Python', str(python), '-Endpoint', f'https://127.0.0.1:{GatewayIntegrationTests.port}',
                       '-ExpectedCaSha256', ca_hash]
                msg = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
                    'name': 'memory_recall', 'arguments': {'query': 'fixture', 'project_tag': 'project:test'}}}
                proc = subprocess.run(cmd, input=json.dumps(msg) + '\n', capture_output=True, text=True, timeout=45)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                result = json.loads(proc.stdout)['result']
                self.assertFalse(result['isError'])
                self.assertIn('fixture', result['content'][0]['text'])
                self.assertNotIn('fixture-' + 'x' * 48, proc.stdout + proc.stderr)
                cmd[-1] = '0' * 64
                rejected = subprocess.run(cmd, input='', capture_output=True, text=True, timeout=45)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn('CA hash mismatch', rejected.stderr)
        finally:
            GatewayIntegrationTests.tearDownClass()
