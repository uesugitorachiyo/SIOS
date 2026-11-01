"""Native Win32 lock and suspended-create/Job-Object process ownership."""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
import msvcrt
import os
from pathlib import Path
import subprocess
import json
import time
import math
import re
import copy
import threading
from ..contract import Blocked, require
from ._windows_support import LOG_LIMIT, publish_native_json, plain_path, digest, handle_identity, directory_identity

K = C.WinDLL('kernel32.dll', use_last_error=True, winmode=0x800)
SIZE = C.c_size_t
HANDLE = W.HANDLE
INVALID = C.c_void_p(-1).value

class IO_COUNTERS(C.Structure):
    _fields_ = [(n, C.c_ulonglong) for n in ('ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]
class BASIC_LIMIT(C.Structure):
    _fields_ = [('PerProcessUserTimeLimit', C.c_longlong), ('PerJobUserTimeLimit', C.c_longlong), ('LimitFlags', W.DWORD), ('MinimumWorkingSetSize', SIZE), ('MaximumWorkingSetSize', SIZE), ('ActiveProcessLimit', W.DWORD), ('Affinity', SIZE), ('PriorityClass', W.DWORD), ('SchedulingClass', W.DWORD)]
class EXTENDED_LIMIT(C.Structure):
    _fields_ = [('BasicLimitInformation', BASIC_LIMIT), ('IoInfo', IO_COUNTERS), ('ProcessMemoryLimit', SIZE), ('JobMemoryLimit', SIZE), ('PeakProcessMemoryUsed', SIZE), ('PeakJobMemoryUsed', SIZE)]
class ACCOUNTING(C.Structure):
    _fields_ = [('TotalUserTime', C.c_longlong), ('TotalKernelTime', C.c_longlong), ('ThisPeriodTotalUserTime', C.c_longlong), ('ThisPeriodTotalKernelTime', C.c_longlong), ('TotalPageFaultCount', W.DWORD), ('TotalProcesses', W.DWORD), ('ActiveProcesses', W.DWORD), ('TotalTerminatedProcesses', W.DWORD)]
class STARTUP(C.Structure):
    _fields_ = [('cb', W.DWORD), ('lpReserved', W.LPWSTR), ('lpDesktop', W.LPWSTR), ('lpTitle', W.LPWSTR), ('dwX', W.DWORD), ('dwY', W.DWORD), ('dwXSize', W.DWORD), ('dwYSize', W.DWORD), ('dwXCountChars', W.DWORD), ('dwYCountChars', W.DWORD), ('dwFillAttribute', W.DWORD), ('dwFlags', W.DWORD), ('wShowWindow', W.WORD), ('cbReserved2', W.WORD), ('lpReserved2', C.POINTER(C.c_ubyte)), ('hStdInput', HANDLE), ('hStdOutput', HANDLE), ('hStdError', HANDLE)]
class STARTUP_EX(C.Structure):
    _fields_ = [("StartupInfo", STARTUP), ("lpAttributeList", C.c_void_p)]

class PROCESS(C.Structure):
    _fields_ = [('hProcess', HANDLE), ('hThread', HANDLE), ('dwProcessId', W.DWORD), ('dwThreadId', W.DWORD)]

def bind(name, restype, args):
    f = getattr(K, name); f.restype = restype; f.argtypes = args; return f
close = bind('CloseHandle', W.BOOL, [HANDLE])
create_job = bind('CreateJobObjectW', HANDLE, [C.c_void_p, W.LPCWSTR])
set_job = bind('SetInformationJobObject', W.BOOL, [HANDLE, C.c_int, C.c_void_p, W.DWORD])
query_job = bind('QueryInformationJobObject', W.BOOL, [HANDLE, C.c_int, C.c_void_p, W.DWORD, C.c_void_p])
assign_job = bind('AssignProcessToJobObject', W.BOOL, [HANDLE, HANDLE])
terminate_job = bind('TerminateJobObject', W.BOOL, [HANDLE, W.UINT])
terminate_process = bind('TerminateProcess', W.BOOL, [HANDLE, W.UINT])
resume = bind('ResumeThread', W.DWORD, [HANDLE])
wait = bind('WaitForSingleObject', W.DWORD, [HANDLE, W.DWORD])
exit_code = bind('GetExitCodeProcess', W.BOOL, [HANDLE, C.POINTER(W.DWORD)])
create_process = bind('CreateProcessW', W.BOOL, [W.LPCWSTR, W.LPWSTR, C.c_void_p, C.c_void_p, W.BOOL, W.DWORD, C.c_void_p, W.LPCWSTR, C.POINTER(STARTUP), C.POINTER(PROCESS)])
create_file = bind('CreateFileW', HANDLE, [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, HANDLE])
open_process = bind('OpenProcess', HANDLE, [W.DWORD, W.BOOL, W.DWORD])
query_image = bind('QueryFullProcessImageNameW', W.BOOL, [HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)])
process_times = bind('GetProcessTimes', W.BOOL, [HANDLE, C.POINTER(W.FILETIME), C.POINTER(W.FILETIME), C.POINTER(W.FILETIME), C.POINTER(W.FILETIME)])
in_job = bind('IsProcessInJob', W.BOOL, [HANDLE, HANDLE, C.POINTER(W.BOOL)])

initialize_attributes = bind('InitializeProcThreadAttributeList', W.BOOL, [C.c_void_p, W.DWORD, W.DWORD, C.POINTER(SIZE)])
update_attributes = bind('UpdateProcThreadAttribute', W.BOOL, [C.c_void_p, W.DWORD, SIZE, C.c_void_p, SIZE, C.c_void_p, C.c_void_p])
delete_attributes = bind('DeleteProcThreadAttributeList', None, [C.c_void_p])

class PROCESS_LIST(C.Structure):
    _fields_=[('assigned',W.DWORD),('listed',W.DWORD),('pids',SIZE*4096)]

def wincheck(value, message):
    if not value:
        raise Blocked(message + ' [winerror=' + str(C.get_last_error()) + ']')
    return value

class RunLock:
    def __init__(self, path):
        self.path = plain_path(path)
    def __enter__(self):
        # Share mode 0 denies overlap; the handle is never inherited. No stale PID lock.
        self.handle = create_file(str(self.path), 0xC0000000, 0, None, 4, 0x80, None)
        if self.handle == INVALID:
            raise Blocked('foreground lock unavailable [winerror=' + str(C.get_last_error()) + ']')
        self.started = self.acquired_monotonic = time.monotonic()
        self.owner_pid = os.getpid()
        self.lock_identity = str(self.path)
        self.file_identity = handle_identity(self.handle)
        return self
    def validate_owned(self):
        require(self.owner_pid == os.getpid() and handle_identity(self.handle) == self.file_identity, "missing_foreground_lease")

    def __exit__(self, *args):
        close(self.handle)
        self.handle = None

class _Stop(Exception):
    """Internal control flow: never serialized as a native exception."""


class Supervisor:
    """Trusted same-user ownership with immutable bounds.

    log_limit_bytes detects observed overshoot; it is not a strict disk quota.
    Native calls and file hashing are not forcibly interruptible at a checkpoint.
    """
    def __init__(self, run, *, productive_allowance_ns, cleanup_allowance_ns,
                 parent_hard_deadline_ns, cap_reference_sha256,
                 productive_deadline_ns, owner_hard_stop_epoch_ns,
                 stop_requested=None, log_limit=LOG_LIMIT, poll=.05):
        def valid(ok, code):
            if not ok:
                e = Blocked(code); e.validation_code = code; e.native_record = None
                raise e
        P, Cn, H = productive_allowance_ns, cleanup_allowance_ns, parent_hard_deadline_ns
        E, U = productive_deadline_ns, owner_hard_stop_epoch_ns
        valid(type(E) is int and E > 0, "productive_deadline_type_or_range")
        valid(type(U) is int and U > 0, "owner_utc_type_or_range")
        valid(type(P) is int and P > 0, 'P_type_or_range')
        valid(type(Cn) is int and Cn >= 0, 'C_type_or_range')
        valid(type(H) is int, 'H_type')
        valid(isinstance(cap_reference_sha256, str) and
              re.fullmatch('[0-9a-f]{64}', cap_reference_sha256) is not None, 'cap_reference')
        valid(isinstance(run, (str, Path)), 'run_type')
        valid(not isinstance(run, str) or bool(run), 'run_empty')
        valid('\0' not in str(run), 'run_nul')
        try:
            self._units(str(run))
        except UnicodeError:
            valid(False, 'run_encoding')
        try:
            self.run = Path(os.path.abspath(run))
        except BaseException as e:
            e.native_record = None; e.validation_code = None
            raise
        valid(self._units(str(self.run)) + 1 <= 260, 'run_length')
        try:
            finite_poll = type(poll) in (int, float) and math.isfinite(poll) and poll >= .001
        except (OverflowError, ValueError):
            finite_poll = False
        valid(finite_poll, 'poll_range')
        valid(type(log_limit) is int and log_limit > 0, 'log_limit')
        valid(stop_requested is None or callable(stop_requested), 'stop_callback')
        t0 = time.monotonic_ns()
        valid(H > t0, 'parent_expired'); valid(H - t0 > Cn, 'cleanup_reserve')
        self.t0, self.P, self.C, self.H = t0, P, Cn, H
        self.H_eff = min(H, t0 + P + Cn)
        self.D = min(t0 + P, self.H_eff - Cn)
        self.D = min(self.D, E)
        valid(self.D > t0, "productive_expired_prelaunch")
        valid(time.time_ns() < U - Cn, "productive_utc_expired_prelaunch")
        self.E, self.U = E, U
        self.live_members = set()
        self.reference = cap_reference_sha256
        self.log_limit, self.poll, self.stop_requested = log_limit, poll, stop_requested
        self.logs = self.run / 'logs'; self._logs_initialized = False; self._logs_identity = None
        self.records = []; self.failed = False; self.unknown = False
        self._guard = threading.Lock()

    @staticmethod
    def _units(s):
        return len(s.encode('utf-16-le')) // 2

    @staticmethod
    def _wall():
        v = time.time()
        return v if math.isfinite(v) else None

    def _record(self):
        r = dict(schema_id='factory-windows-native-timing-record-05', record_kind='authoritative_final',
                 call_index=len(self.records) + 1, cap_reference_sha256=self.reference,
                 t0_ns=self.t0, productive_allowance_ns=self.P, cleanup_allowance_ns=self.C,
                 parent_hard_deadline_ns=self.H, D_ns=self.D, H_eff_ns=self.H_eff,
                 productive_remaining_entry_ns=0, final_observed_ns=None,
                 first_stop_reason=None, validation_code=None, launch_status='not_started',
                 known_ended=True, pid=None, exit_code=None, active_processes_final=None,
                 assigned_before_resume=False, operational_success=False, terminal_instance=False,
                 quality_verdict='not_assessed', cleanup_outcome='not_needed', cleanup_deadline_ns=None,
                 termination_attempts=0, primary_exception=None, secondary_exceptions=[],
                 secondary_exception_overflow_count=0, publication_status='pending',
                 publication_pre_ns=None, publication_post_ns=None, publication_check_ns=None,
                 publication_crossed_D=False, publication_crossed_H_eff=False, publication_failure=None,
                 durable_path=None, durable_record_claim='provisional_only_or_absent',
                 poll_seconds=self.poll, log_limit_bytes=self.log_limit, argv=[], cwd=None, role=None,
                 stdout=None, stderr=None, callback_observation=dict(count=0,total_elapsed_ns=0,
                 last_point=None,last_stop_reference=None), observed_job_processes=[],
                 observation_scope='Owned Job PID/creation/image samples; not every transient descendant or vendor cancellation; image digest is file-at-path bytes',
                 cap_trigger=None, observed_member_overflow_count=0, started_at_unix=None,
                 ended_at_unix=None, max_observed_active_processes=0,
                 surviving_job_count_at_root_exit=None, cleanup_zero_wait_probe='not_needed',
                 absolute_productive_deadline_ns=self.E, owner_hard_stop_epoch_ns=self.U,
                 productive_utc_deadline_epoch_ns=self.U-self.C, last_utc_observed_ns=time.time_ns(), first_cap_clock=None)
        self.records.append(r)
        return r

    def _cause(self, r, reason, trigger=None):
        if r['first_stop_reason'] is None:
            r['first_stop_reason'] = reason
            if reason == 'productive_cap':
                r['cap_trigger'] = trigger or 'observed_deadline'
                r['first_cap_clock'] = 'monotonic' if trigger == 'submillisecond_no_wait_budget' or time.monotonic_ns() >= self.D else 'utc'
        self.failed = True; r['terminal_instance'] = True

    def _check(self, r):
        r['last_utc_observed_ns'] = time.time_ns()
        if time.monotonic_ns() >= self.D or r['last_utc_observed_ns'] >= self.U-self.C:
            self._cause(r, 'productive_cap')
        if r['first_stop_reason'] is not None: raise _Stop()

    def _step(self, r, fn, *args):
        self._check(r)
        value = fn(*args)
        self._check(r)
        return value

    def _checkpoint(self, r, point):
        self._check(r)
        if self.stop_requested is None: return
        stat = r['callback_observation']; stat['count'] += 1; stat['last_point'] = point
        start = time.monotonic_ns()
        self._region = 'callback'
        try:
            value = self.stop_requested()
        finally:
            stat['total_elapsed_ns'] += max(0, time.monotonic_ns()-start)
        self._check(r)
        if value is not None:
            if not (type(value) is tuple and len(value)==2 and value[0]=='caller_stop' and
                    isinstance(value[1],str) and re.fullmatch('[0-9a-f]{64}',value[1])):
                raise Blocked('invalid callback result')
            stat['last_stop_reference'] = value[1]
            self._cause(r, 'caller_stop'); raise _Stop()
        self._region = 'native'

    @staticmethod
    def _error(e, category):
        if category == 'callback':
            message = 'callback exception text omitted'
        else:
            try: message = str(e)
            except BaseException: message = 'exception text unavailable'
            try:
                home = str(Path.home())
            except (RuntimeError, OSError):
                home = None
            if home:message = re.sub(re.escape(home), '<OWNER_HOME>', message, flags=re.I)
            message = re.sub(r'(?i)[A-Z]:[\\/]Users[\\/][^\\/\s\'\"]+', '<OWNER_HOME>', message)
            message = re.sub(r'(?i)Bearer\s+\S+|sk-[A-Za-z0-9_-]+|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '<REDACTED>', message)
            message = message.encode('utf-8','replace')[:2048].decode('utf-8','ignore')
        def numeric(name):
            try: v = getattr(e,name,None)
            except BaseException: return None
            return v if type(v) is int else None
        return dict(category=category,type=type(e).__name__[:128],message=message,
                    errno=numeric('errno'),winerror=numeric('winerror'))

    def _capture(self, r, e, category, *, secondary=False):
        fact = self._error(e, category)
        if secondary:
            if len(r['secondary_exceptions'])<64: r['secondary_exceptions'].append(fact)
            else: r['secondary_exception_overflow_count'] = min(2147483647,r['secondary_exception_overflow_count']+1)
        else: r['primary_exception'] = fact
        if time.monotonic_ns() >= self.D or time.time_ns() >= self.U-self.C: self._cause(r,'productive_cap')
        elif isinstance(e,KeyboardInterrupt): self._cause(r,'interrupted')
        elif category=='validation' and isinstance(e,Blocked): self._cause(r,'prelaunch_rejected')
        elif category=='publication': self._cause(r,'publication_error')
        else: self._cause(r,'native_error')
        return fact

    def _validate(self, r, argv, cwd, env, input_bytes, success, role):
        self._region = 'validation'
        def v(ok,code):
            if not ok:
                r['validation_code']=code
                e=Blocked(code);e.validation_code=code
                raise e
        def enc(s):
            try:self._units(s);return True
            except UnicodeError:return False
        v(isinstance(argv,(list,tuple)),'argv_type');v(bool(argv),'argv_empty')
        v(all(isinstance(x,str) for x in argv),'argv_item_type')
        v(all('\0' not in x for x in argv),'argv_nul')
        v(all(enc(x) for x in argv),'argv_encoding')
        command=subprocess.list2cmdline(argv)
        v(self._units(command)+1<=32767,'command_length')
        v(Path(argv[0]).is_absolute() and Path(argv[0]).suffix.casefold()=='.exe','executable_path')
        v(isinstance(cwd,(str,Path)),'cwd_type');v(not isinstance(cwd,str) or bool(cwd),'cwd_empty')
        v('\0' not in str(cwd),'cwd_nul');v(enc(str(cwd)),'cwd_encoding')
        cwd=Path(os.path.abspath(cwd));v(self._units(str(cwd))+1<=260,'cwd_length')
        v(isinstance(env,dict),'env_type')
        keys=list(env)
        v(all(isinstance(k,str) for k in keys),'env_key_type')
        v(all(isinstance(x,str) for x in env.values()),'env_value_type')
        v(all(bool(k) for k in keys),'env_key_empty')
        v(all('\0' not in k for k in keys),'env_key_nul')
        v(all('\0' not in x for x in env.values()),'env_value_nul')
        v(all('=' not in k for k in keys),'env_key_equals')
        v(all(enc(k) and enc(env[k]) for k in keys),'env_encoding')
        v(len({k.upper() for k in keys})==len(keys) and len({k.casefold() for k in keys})==len(keys),'env_key_alias')
        v(not any(k.upper()=='S0_PROCESS_REGISTRY' or k.casefold()=='s0_process_registry' for k in keys),'registry_unsupported')
        v(input_bytes is None or isinstance(input_bytes,bytes),'input_bytes_type')
        v(type(success) is bool,'success_type');v(role is None or isinstance(role,str),'role_type')
        v(role is None or len(role)<=128,'role_length')
        prefix=self.logs/('%04d'%r['call_index'])
        paths={ext:prefix.with_suffix('.'+ext) for ext in ['stdin','stdout','stderr','json','json.partial']}
        v(all(self._units(str(x))+1<=260 for x in [self.logs,*paths.values()]),'log_path_length')
        try:
            for x in [self.run,cwd,self.logs,*paths.values(),Path(argv[0])]:plain_path(x)
        except Blocked:
            r['validation_code']='path_policy';raise
        v(self.run.is_dir(),'run_directory')
        r.update(argv=list(argv),cwd=str(cwd),role=role,stdout=str(paths['stdout']),
                 stderr=str(paths['stderr']),durable_path=str(paths['json']))
        return cwd,dict(env),paths

    def active(self, job):
        info=ACCOUNTING()
        wincheck(query_job(job,1,C.byref(info),C.sizeof(info),None),'Job enumeration unavailable')
        return info.ActiveProcesses

    def _wait(self,r,handle,window_ns=None):
        self._check(r)
        remaining=self.D-time.monotonic_ns()
        ns=min(remaining,int(self.poll*1_000_000_000))
        if window_ns is not None: ns=min(ns,window_ns)
        ms=max(0,ns//1_000_000)
        status=wait(handle,min(ms,0xfffffffe))
        self._check(r)
        wincheck(status in (0,258),'process wait unavailable')
        if status==258 and remaining<1_000_000:
            self._cause(r,'productive_cap','submillisecond_no_wait_budget');raise _Stop()
        return status

    def observe_job(self, job, observed, r):
        members=PROCESS_LIST()
        self._step(r,lambda:wincheck(query_job(job,3,C.byref(members),C.sizeof(members),None),'Job process list unavailable/oversized'))
        if members.listed>4096:
            r['observed_member_overflow_count']=1;self._cause(r,'native_error');raise _Stop()
        self.live_members = set()
        for pid in members.pids[:members.listed]:
            self._check(r)
            handle=open_process(0x1000|0x100000,False,pid)
            if not handle:
                self._check(r)
                wincheck(C.get_last_error() in (87,1168),'owned process observation denied')
                continue
            try:
                self._check(r)
                owned=W.BOOL()
                self._step(r,lambda:wincheck(in_job(handle,job,C.byref(owned)),'Job membership observation failed'))
                if not owned.value:continue
                times=[W.FILETIME() for _ in range(4)]
                self._step(r,lambda:wincheck(process_times(handle,*[C.byref(t) for t in times]),'owned process times unavailable'))
                born=(times[0].dwHighDateTime<<32)|times[0].dwLowDateTime
                key=(int(pid),born)
                live_status = wait(handle,0)
                wincheck(live_status in (0,258), "owned live sample unavailable")
                if live_status == 258:self.live_members.add(key)
                if key in observed:continue
                if len(observed)>=4096:
                    r['observed_member_overflow_count']=1;self._cause(r,'native_error');raise _Stop()
                buf=C.create_unicode_buffer(32768);n=W.DWORD(len(buf))
                self._check(r);ok=query_image(handle,0,buf,C.byref(n));self._check(r)
                image=None;image_hash=None;note='file-at-path bytes sampled, not mapped-image identity'
                if not ok:
                    status=self._wait(r,handle,250_000_000)
                    wincheck(status==0,'live owned process image observation denied')
                    note='process ended before image query; no executable identity claimed'
                else:
                    image=buf.value;image_hash=self._step(r,digest,image)
                row=dict(pid=int(pid),creation_filetime=born,image=image,image_sha256=image_hash,
                         image_observation=note,membership='this native Job only')
                observed[key]=row;r['observed_job_processes'].append(row)
            finally:
                try:wincheck(close(handle),'owned observation handle close failed')
                except BaseException as e:self._secondary(e,'finalization')
            self._check(r)

    def _sizes(self,r,paths):
        for name in ['stdout','stderr']:
            size=self._step(r,lambda:paths[name].stat().st_size)
            if size>self.log_limit:
                self._cause(r,'output_limit');raise _Stop()

    def _settle(self,r,job,root,observed):
        until=min(self.D,time.monotonic_ns()+250_000_000)
        while True:
            self.observe_job(job,observed,r)
            self._checkpoint(r,'settle')
            count=self._step(r,self.active,job)
            if count==0:return
            now=time.monotonic_ns();self._check(r)
            if now>=until:
                r['surviving_job_count_at_root_exit']=count
                self._cause(r,'descendants_after_exit');raise _Stop()
            remaining=min(until-now,self.D-now)
            if remaining<1_000_000:
                # One nonblocking sample of the local settle-window end, not a spin.
                count=self._step(r,self.active,job)
                if count:
                    r['surviving_job_count_at_root_exit']=count
                    if self.D-now<1_000_000:self._cause(r,'productive_cap','submillisecond_no_wait_budget')
                    else:self._cause(r,'descendants_after_exit')
                    raise _Stop()
                return
            self._step(r,time.sleep,min(5_000_000,remaining)//1_000_000/1000)

    def _cleanup(self,r,job,process,secondary):
        if r['cleanup_deadline_ns'] is not None:return
        r['cleanup_deadline_ns']=min(time.monotonic_ns()+self.C,self.H_eff)
        if not process.hProcess:
            r['cleanup_outcome']='known_ended' if r['known_ended'] else 'unknown'
            return
        try:
            r['termination_attempts']=1
            if r['assigned_before_resume']:
                wincheck(terminate_job(job,125),'Job termination failed')
            else:wincheck(terminate_process(process.hProcess,125),'suspended child termination failed')
        except BaseException as e:
            secondary(e,'cleanup')
        # Exactly one end probe is permitted even when cleanup time is already exhausted.
        try:
            r['cleanup_zero_wait_probe']='performed'
            status=wait(process.hProcess,0)
            wincheck(status in (0,258),'cleanup process wait unavailable')
            ended=status==0
            active=self.active(job) if r['assigned_before_resume'] else (0 if ended else 1)
            r['active_processes_final']=active
            while not (ended and active==0) and time.monotonic_ns()<r['cleanup_deadline_ns'] and time.time_ns()<self.U:
                remain=min(r['cleanup_deadline_ns']-time.monotonic_ns(),self.U-time.time_ns())
                if remain<1_000_000:break
                time.sleep(min(5_000_000,remain)//1_000_000/1000)
                status=wait(process.hProcess,0)
                wincheck(status in (0,258),'cleanup process wait unavailable')
                ended=status==0
                active=self.active(job) if r['assigned_before_resume'] else (0 if ended else 1)
                r['active_processes_final']=active
            if ended and active==0:r['known_ended']=True
            r['cleanup_outcome']='known_ended' if r['known_ended'] else 'unknown'
        except BaseException as e:
            r['cleanup_zero_wait_probe']='failed';r['cleanup_outcome']='unknown'
            secondary(e,'cleanup')
        self.unknown=not r['known_ended']

    def _publish(self,r,secondary):
        pre=time.monotonic_ns();r['publication_pre_ns']=pre
        if pre>=self.D or time.time_ns()>=self.U-self.C:self._cause(r,'productive_cap')
        if not self._logs_initialized or self._logs_identity is None:
            r['publication_status']='not_attempted_no_owned_dir';check=pre
        elif pre>=self.H_eff or time.time_ns()>=self.U:
            r['publication_status']='not_attempted_H_eff';check=pre
        else:
            try:
                payload=copy.deepcopy(r)
                payload.update(record_kind='provisional_prepublication',operational_success=None,
                               publication_status='pending',publication_post_ns=None,final_observed_ns=None)
                if r['durable_path'] is None:raise Blocked('native record path unavailable')
                publish_native_json(Path(r['durable_path']),payload,self._logs_identity)
                r['publication_status']='ok'
            except BaseException as e:
                r['publication_status']='error'
                r['publication_failure']=self._error(e,'publication')
                secondary(e,'publication')
            check=time.monotonic_ns();r['publication_post_ns']=check
        r['publication_check_ns']=check
        r['publication_crossed_D']=check>=self.D
        r['publication_crossed_H_eff']=check>=self.H_eff
        if check>=self.D or time.time_ns()>=self.U-self.C:self._cause(r,'productive_cap')

    def call(self,argv,*,cwd,env,input_bytes=None,success=True,role=None):
        if self.failed or self.unknown:
            e=Blocked('terminal instance');e.validation_code='terminal_instance'
            e.native_record=self.records[-1] if self.records else None;raise e
        if not self._guard.acquire(False):
            e=Blocked('reentrant call');e.validation_code='reentrant_call';e.native_record=None;raise e
        if self.failed or self.unknown:
            self._guard.release()
            e=Blocked('terminal instance');e.validation_code='terminal_instance'
            e.native_record=self.records[-1] if self.records else None;raise e
        try:
            r=self._record();self._region='native'
        except BaseException:
            self.failed=True;self.unknown=True;self._guard.release()
            raise
        job=None;process=PROCESS();streams=[];observed={}
        primary=None;secondary_first=None
        def secondary(e,category):
            nonlocal secondary_first
            if secondary_first is None:secondary_first=e
            self._capture(r,e,category,secondary=True)
        self._secondary=secondary
        try:
            try:
                r['productive_remaining_entry_ns']=max(0,self.D-time.monotonic_ns())
                self._check(r)
                cwd,env,paths=self._validate(r,argv,cwd,env,input_bytes,success,role)
                self._checkpoint(r,'entry');self._region='native'
                if not self._logs_initialized:
                    self._step(r,self.logs.mkdir)
                    self._logs_identity=self._step(r,directory_identity,self.logs)
                    self._logs_initialized=True
                r['started_at_unix']=self._wall()
                self._check(r);job=wincheck(create_job(None,None),'Job creation unavailable');self._check(r)
                limit=EXTENDED_LIMIT();limit.BasicLimitInformation.LimitFlags=0x2000
                self._step(r,lambda:wincheck(set_job(job,9,C.byref(limit),C.sizeof(limit)),'Job kill-on-close unavailable'))
                def write_input():
                    with paths['stdin'].open('xb') as stream:stream.write(input_bytes or b'')
                self._step(r,write_input)
                for name,mode in [('stdin','rb'),('stdout','xb'),('stderr','xb')]:
                    self._check(r);streams.append(paths[name].open(mode));self._check(r)
                handles=[msvcrt.get_osfhandle(s.fileno()) for s in streams]
                startup=STARTUP_EX();startup.StartupInfo.cb=C.sizeof(startup);startup.StartupInfo.dwFlags=0x100
                startup.StartupInfo.hStdInput,startup.StartupInfo.hStdOutput,startup.StartupInfo.hStdError=handles
                attribute_size=SIZE(0)
                self._step(r,initialize_attributes,None,1,0,C.byref(attribute_size))
                wincheck(0<attribute_size.value<=65536 and C.get_last_error()==122,'stdio attribute size unavailable')
                storage=C.create_string_buffer(attribute_size.value)
                startup.lpAttributeList=C.cast(storage,C.c_void_p)
                attributes_initialized=False;inheritable=list(handles)
                try:
                    self._check(r)
                    wincheck(initialize_attributes(startup.lpAttributeList,1,0,C.byref(attribute_size)),'stdio attributes unavailable')
                    attributes_initialized=True
                    self._check(r)
                    allowed=(HANDLE*3)(*handles)
                    self._step(r,lambda:wincheck(update_attributes(startup.lpAttributeList,0,0x20002,C.cast(allowed,C.c_void_p),C.sizeof(allowed),None,None),'stdio handle list unavailable'))
                    for h in handles:
                        self._step(r,os.set_handle_inheritable,h,True)
                    command=C.create_unicode_buffer(subprocess.list2cmdline(argv))
                    block=C.create_unicode_buffer('\0'.join(k+'='+v for k,v in sorted(env.items(),key=lambda kv:kv[0].upper()))+'\0\0')
                    self._checkpoint(r,'precreate');self._region='native'
                    r['launch_status']='unknown';r['known_ended']=False
                    launched=create_process(str(argv[0]),command,None,None,True,0x4|0x200|0x400|0x08000000|0x80000,block,str(cwd),C.cast(C.byref(startup),C.POINTER(STARTUP)),C.byref(process))
                    if not launched and not process.hProcess:
                        r['launch_status']='not_started';r['known_ended']=True
                    if process.hProcess:r['pid']=process.dwProcessId;r['launch_status']='started_suspended'
                    wincheck(launched,'Suspended process creation failed');self._check(r)
                finally:
                    for h in inheritable:
                        try:os.set_handle_inheritable(h,False)
                        except BaseException as e:secondary(e,'finalization')
                    if attributes_initialized:
                        try:delete_attributes(startup.lpAttributeList)
                        except BaseException as e:secondary(e,'finalization')
                wincheck(assign_job(job,process.hProcess),'Job assignment failed; child never resumed')
                r['assigned_before_resume']=True;self._check(r)
                self.observe_job(job,observed,r)
                self._checkpoint(r,'preresume');self._region='native'
                wincheck(resume(process.hThread)!=0xffffffff,'process resume failed')
                r['launch_status']='started';self._check(r)
                while True:
                    self.observe_job(job,observed,r)
                    self._checkpoint(r,'monitor');self._region='native'
                    active=self._step(r,self.active,job)
                    r['max_observed_active_processes']=max(active,r['max_observed_active_processes'])
                    self.observe_job(job,observed,r);self._sizes(r,paths)
                    if self._wait(r,process.hProcess)==0:
                        code=W.DWORD();self._step(r,lambda:wincheck(exit_code(process.hProcess,C.byref(code)),'exit code unavailable'))
                        r['exit_code']=code.value
                        if code.value!=0:
                            self._cause(r,'nonzero_exit');raise _Stop()
                        self._settle(r,job,process.hProcess,observed);self._sizes(r,paths)
                        r['known_ended']=True;r['active_processes_final']=0
                        break
            except _Stop:pass
            except BaseException as e:
                primary=e
                cat='interrupt' if isinstance(e,KeyboardInterrupt) else self._region
                self._capture(r,e,cat)
            if r['first_stop_reason'] is not None:self._cleanup(r,job,process,secondary)
            for stream in streams:
                try:stream.close()
                except BaseException as e:secondary(e,'finalization')
            if r['first_stop_reason'] is not None:self._cleanup(r,job,process,secondary)
            self._publish(r,secondary)
            if r['first_stop_reason'] is not None:self._cleanup(r,job,process,secondary)
        finally:
            try:
                for name,h in [('thread',process.hThread),('process',process.hProcess),('job',job)]:
                    if h:
                        try:wincheck(close(h),'owned '+name+' handle close failed')
                        except BaseException as e:secondary(e,'finalization')
                try:r['ended_at_unix']=self._wall()
                except BaseException as e:secondary(e,'finalization')
                now=time.monotonic_ns();r['final_observed_ns']=now
                r['last_utc_observed_ns']=time.time_ns()
                if now>=self.D or r['last_utc_observed_ns']>=self.U-self.C:self._cause(r,'productive_cap')
                if not r['known_ended'] or r['secondary_exception_overflow_count'] or r['observed_member_overflow_count']:
                    self._cause(r,'native_error')
                if r['publication_status']!='ok' and r['first_stop_reason'] is None:self._cause(r,'publication_error')
                self.unknown=not r['known_ended']
                r['operational_success']=r['first_stop_reason'] is None and r['known_ended'] and r['exit_code']==0 and r['publication_status']=='ok' and now<self.D and time.time_ns()<self.U-self.C
                if not r['operational_success']:self.failed=True;r['terminal_instance']=True
            except BaseException as final_error:
                self.failed=True;self.unknown=True
                r['operational_success']=False;r['terminal_instance']=True
                r['final_observed_ns']=None;r['cleanup_outcome']='unknown'
                if r['first_stop_reason'] is None:r['first_stop_reason']='native_error'
                if secondary_first is None:secondary_first=final_error
                if len(r['secondary_exceptions'])<64:
                    r['secondary_exceptions'].append(dict(category='finalization',type=type(final_error).__name__[:128],message='finalization interrupted; final observation unknown',errno=None,winerror=None))
                else:r['secondary_exception_overflow_count']=min(2147483647,r['secondary_exception_overflow_count']+1)
            finally:
                self._guard.release()
        error=primary or secondary_first
        if error is not None:
            error.native_record=r
            raise error
        if not r['operational_success'] and not (r['first_stop_reason']=='nonzero_exit' and success is False and r['known_ended'] and r['publication_status']=='ok' and r['final_observed_ns']<self.D):
            if r['first_stop_reason'] is None:self._cause(r,'native_error')
            e=Blocked(r['first_stop_reason']);e.native_record=r;e.validation_code=r['validation_code'];raise e
        return r
