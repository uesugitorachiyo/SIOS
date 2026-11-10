import sys
import unittest
if sys.platform != 'darwin':
    raise unittest.SkipTest('macOS-specific sandbox process fixture')
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from tests.repair_process_fixture import available

SUPERVISOR=r'''
import os,sys,time
from pathlib import Path
from repair_process_fixture import launch_with_lock
root=Path(sys.argv[1])
program="import os,sys,time; from pathlib import Path; p=Path(sys.argv[1]); (p/'child').write_text(str(os.getpid())); time.sleep(2)"
fd,child=launch_with_lock(root/'fixture.lock',['/usr/bin/sandbox-exec','-p','(version 1) (allow default) (deny network*)',sys.executable,'-c',program,str(root)])
(root/'ready').write_text(str(child.pid))
time.sleep(20)
'''

class RepairProcessFixture(unittest.TestCase):
    def test_supervisor_death_does_not_release_live_child_lock(self):
        with tempfile.TemporaryDirectory(prefix='sios-process-fixture-') as name:
            root=Path(name).resolve()
            env = dict(os.environ)
            env['PYTHONPATH'] = str(Path(__file__).resolve().parent) + os.pathsep + env.get('PYTHONPATH', '')
            supervisor=subprocess.Popen([sys.executable,'-c',SUPERVISOR,str(root)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,env=env)
            self.addCleanup(lambda: supervisor.kill() if supervisor.poll() is None else None)
            self.addCleanup(supervisor.stderr.close)
            deadline=time.monotonic()+5
            while not (root/'child').exists() and time.monotonic()<deadline:
                if supervisor.poll() is not None: self.fail(supervisor.communicate()[1].decode())
                time.sleep(.02)
            self.assertTrue((root/'child').exists())
            supervisor.kill(); supervisor.wait(timeout=5)
            self.assertFalse(available(root/'fixture.lock'))
            deadline=time.monotonic()+5
            while not available(root/'fixture.lock') and time.monotonic()<deadline: time.sleep(.05)
            self.assertTrue(available(root/'fixture.lock'))
