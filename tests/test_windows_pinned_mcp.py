"""Windows launcher tests using a private temporary runtime and synthetic token."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import venv


@unittest.skipUnless(os.name == 'nt', 'requires Windows and DPAPI')
class WindowsPinnedLauncherTests(unittest.TestCase):
    def test_pinned_profile_and_tampering(self):
        template = Path(__file__).resolve().parents[1] / 'templates/windows-pinned-mcp.py'
        with tempfile.TemporaryDirectory(prefix='sios pinned ') as temp:
            root = Path(temp) / 'nested space \u30c6\u30b9\u30c8'
            root.mkdir()
            runtime = root / 'runtime'
            venv.create(runtime, with_pip=False, system_site_packages=True)
            python = runtime / 'Scripts/python.exe'
            site = runtime / 'Lib/site-packages'
            for name in ('mcp', 'pydantic'):
                metadata = site / (name + '-1.0.dist-info')
                metadata.mkdir()
                (metadata / 'METADATA').write_text('Name: ' + name + '\nVersion: 1.0\n', encoding='utf-8')
            pkg = root / 'package/aios'
            pkg.mkdir(parents=True)
            (pkg / '__init__.py').write_text('', encoding='utf-8')
            (pkg / 'memory_mcp.py').write_text(
                'import os,sys\n'
                'assert os.environ.get("SIOS_TEST_TOKEN")=="x"*40\n'
                'assert sys.argv[1]=="--config" and sys.argv[3]=="--ca-file" and sys.argv[5]=="mcp"\n'
                'print("synthetic child started",flush=True)\n', encoding='utf-8')
            hashfile = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
            manifest = {'aios/' + p.name: hashfile(p) for p in pkg.iterdir()}
            (root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            (root / 'ca.crt').write_bytes(b'synthetic ca')
            cfg = {'mode': 'client', 'instance_id': '01234567-89ab-cdef-0123-456789abcdef',
                   'endpoint': 'https://example.invalid:1234', 'token_env': 'SIOS_TEST_TOKEN'}
            (root / 'config.json').write_text(json.dumps(cfg), encoding='utf-8')
            powershell = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
            token_script = root / 'make-token.ps1'
            token_script.write_text('param($target) $ErrorActionPreference="Stop"; '
                'Import-Module (Join-Path $PSHOME "Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1"); '
                '$s=ConvertTo-SecureString ("x" * 40) -AsPlainText -Force; '
                '[IO.File]::WriteAllText($target,(ConvertFrom-SecureString $s),[Text.Encoding]::ASCII)', encoding='utf-8')
            subprocess.run([powershell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(token_script),
                            str(root / 'token.dpapi')], check=True, capture_output=True)
            profile = {'root': str(root), 'runtime': 'runtime/Scripts/python.exe', 'runtime_sha256': hashfile(python),
                       'config_file': 'config.json', 'ca_file': 'ca.crt', 'secret_file': 'token.dpapi',
                       'package_root': 'package', 'manifest_file': 'manifest.json',
                       'manifest_sha256': hashfile(root / 'manifest.json'), 'ca_sha256': hashfile(root / 'ca.crt'),
                       'config': cfg, 'module': 'aios.memory_mcp',
                       'distributions': {'mcp': '1.0', 'pydantic': '1.0'}}
            profile_path = root / 'profile.json'
            profile_path.write_text(json.dumps(profile), encoding='utf-8')
            args = [str(python), '-I', '-S', '-B', str(template), '--profile', str(profile_path),
                    '--profile-sha256', hashfile(profile_path)]
            result = subprocess.run(args, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'synthetic child started')
            hostile = root / 'hostile working directory'
            (hostile / 'aios').mkdir(parents=True)
            (hostile / 'aios/__init__.py').write_text('', encoding='utf-8')
            (hostile / 'aios/memory_mcp.py').write_text(
                'import os\nprint("HOSTILE:" + os.environ.get("SIOS_TEST_TOKEN", ""))\n', encoding='utf-8')
            result = subprocess.run(args, cwd=hostile, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'synthetic child started')
            self.assertNotIn('HOSTILE', result.stdout)
            for target in (root / 'ca.crt', pkg / 'memory_mcp.py'):
                before = target.read_bytes()
                target.write_bytes(before + b'\nchanged')
                result = subprocess.run(args, capture_output=True, text=True, timeout=30)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('synthetic child started', result.stdout)
                target.write_bytes(before)
            bad_pin = args.copy()
            bad_pin[-1] = '0' * 64
            result = subprocess.run(bad_pin, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('synthetic child started', result.stdout)


if __name__ == '__main__':
    unittest.main()
