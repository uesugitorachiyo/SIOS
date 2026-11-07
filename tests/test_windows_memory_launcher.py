"""Native Windows checks with disposable synthetic credentials and a fake child."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == 'nt', 'requires Windows PowerShell and DPAPI')
class WindowsMemoryLauncherTests(unittest.TestCase):
    def test_identity_pins_and_bounded_child_arguments(self):
        launcher = Path(__file__).resolve().parents[1] / 'templates' / 'windows-memory-client.ps1'
        with tempfile.TemporaryDirectory(prefix='sios satellite ') as temporary:
            root = Path(temporary) / 'nested space \u30c6\u30b9\u30c8'
            root.mkdir()
            marker = root / 'child-ran.txt'
            child = root / 'fake-memory.exe'
            source = root / 'fake.cs'
            source.write_text('''using System; using System.IO;
public class Fake { public static int Main(string[] a) {
  if (Environment.GetEnvironmentVariable("SIOS_TEST_TOKEN") != new string('x', 40)) return 8;
  if (a.Length != 5 || a[0] != "--config" || a[2] != "--ca-file" || a[4] != "mcp") return 9;
  File.WriteAllText(Environment.GetEnvironmentVariable("SIOS_TEST_MARKER"), "called"); return 0;
}}''', encoding='utf-8')
            env = dict(os.environ, SIOS_TEST_MARKER=str(marker))
            ps = str(Path(os.environ['SystemRoot']) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe')
            # Paths are passed as arguments to PowerShell, never interpolated into code.
            setup = ('param($src,$out,$token) $ErrorActionPreference="Stop"; '
                     'Import-Module (Join-Path $PSHOME "Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1"); '
                     'Add-Type -Path $src -OutputAssembly $out -OutputType ConsoleApplication; '
                     '$s=ConvertTo-SecureString ("x" * 40) -AsPlainText -Force; '
                     '[IO.File]::WriteAllText($token,(ConvertFrom-SecureString $s),[Text.Encoding]::ASCII)')
            setup_script = root / 'setup.ps1'
            setup_script.write_text(setup, encoding='utf-8')
            setup_result = subprocess.run([ps, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(setup_script), str(source), str(child),
                            str(root / 'satellite-token.dpapi')], env=env, capture_output=True, text=True)
            self.assertEqual(setup_result.returncode, 0, setup_result.stderr)
            self.assertTrue((root / 'satellite-token.dpapi').exists(), setup_result.stderr)
            ca = b'synthetic CA bytes for launcher validation'
            (root / 'ca.crt').write_bytes(ca)
            instance = '01234567-89ab-cdef-0123-456789abcdef'
            endpoint = 'https://example.invalid:1234'
            (root / 'config.json').write_text(json.dumps({'mode': 'client', 'instance_id': instance,
                'endpoint': endpoint, 'token_env': 'SIOS_TEST_TOKEN'}), encoding='utf-8')
            args = [ps, '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(launcher), '-SetupRoot', str(root),
                    '-SiosExe', str(child), '-ExpectedInstanceId', instance, '-ExpectedEndpoint', endpoint,
                    '-ExpectedCaSha256', hashlib.sha256(ca).hexdigest()]
            result = subprocess.run(args, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(marker.exists())
            marker.unlink()
            for index, replacement in ((args.index('-ExpectedCaSha256') + 1, '0' * 64),
                                       (args.index('-ExpectedInstanceId') + 1, '1' * 8 + instance[8:]),
                                       (args.index('-ExpectedEndpoint') + 1, 'https://other.invalid:1234')):
                invalid = args.copy()
                invalid[index] = replacement
                result = subprocess.run(invalid, env=env, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(marker.exists(), 'mismatched pin must not launch the child')


if __name__ == '__main__':
    unittest.main()
