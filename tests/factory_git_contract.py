"""Explicitly invoked Git fixture; supply FACTORY_TEST_GIT and supervise externally."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from aios.factory.platforms.git_capture import tree_identity


class TrackedTreeGitTests(unittest.TestCase):
    def test_dotfiles_match_real_git_write_tree(self):
        git = os.environ.get('FACTORY_TEST_GIT', '')
        self.assertTrue(Path(git).is_absolute(), 'FACTORY_TEST_GIT must be an explicit absolute executable')
        with tempfile.TemporaryDirectory(prefix='factory-git-', dir=Path(tempfile.gettempdir()).resolve()) as name:
            root = Path(name); baseline = root / 'baseline'; baseline.mkdir()
            (baseline / '.gitignore').write_bytes(b'ignored.txt\n')
            (baseline / 'ignored.txt').write_bytes(b'tracked anyway\n')
            (baseline / '.hidden').mkdir(); (baseline / '.hidden' / 'file').write_bytes(b'hidden tracked bytes\n')
            hooks = root / 'hooks'; hooks.mkdir(); meta = root / 'capture.git'
            env = {'PATH': os.defpath, 'HOME': str(root), 'GIT_CONFIG_NOSYSTEM': '1',
                   'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_ATTR_NOSYSTEM': '1',
                   'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0'}
            if os.name == 'nt' and os.environ.get('SystemRoot'):
                env['SystemRoot'] = os.environ['SystemRoot']
                env['PATH'] = str(Path(env['SystemRoot']) / 'System32')
            def invoke(*args):
                return subprocess.run([git, *args], cwd=root, env=env, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=10).stdout
            invoke('init', '--bare', str(meta))
            common = ['-c', 'core.hooksPath=' + str(hooks), '-c', 'core.attributesFile=' + os.devnull,
                      '-c', 'core.autocrlf=false', '-c', 'core.filemode=' + ('false' if os.name == 'nt' else 'true'),
                      '--git-dir=' + str(meta), '--work-tree=' + str(baseline), '-C', str(baseline)]
            invoke(*common, 'add', '-f', '-A', '--', '.')
            actual = invoke(*common, 'write-tree').decode('ascii').strip()
            self.assertEqual(actual, tree_identity(baseline))
