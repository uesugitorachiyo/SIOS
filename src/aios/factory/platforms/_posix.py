"""One foreground supervisor for every child, including Git and checks."""
from __future__ import annotations

import fcntl
import ctypes as C
import hashlib
import sys
import threading
import os
from pathlib import Path
import signal
import subprocess
import time
import psutil
import math
from datetime import datetime, timezone, timedelta

from ..contract import Blocked, require
import json

LOG_LIMIT = 20 * 1024 * 1024

def require_reaping_custody():
    """Per project-child read-only check; exclusive reaping is an entry contract.

    A trusted, single-thread caller must not call external waitpid or load a
    reaping agent. Native extensions outside that contract are unsupported.
    No Python query can prove absence of arbitrary external native waiters.
    """
    code = "mac_reaping_custody_unknown"
    try:
        require(sys.platform == "darwin" and sys.implementation.name == "cpython" and
                sys.version_info[:3] == (3, 12, 13) and C.sizeof(C.c_void_p) == 8 and int(signal.SIGCHLD) == 20, code)
        # The caller binds the selected executable and records its actual
        # stdlib origin. The custody check runs on every supervised entry.
        require(isinstance(subprocess.Popen, type) and subprocess.Popen.__module__ == "subprocess" and
                subprocess.Popen.__name__ == "Popen" and
                Path(subprocess.Popen.__init__.__code__.co_filename).resolve() == Path(subprocess.__file__).resolve(), code)
        require(subprocess._active == [] and threading.active_count() == 1 and
                threading.current_thread() is threading.main_thread() and
                signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL, code)
        class Sigaction(C.Structure):
            _fields_ = [("handler", C.c_void_p), ("mask", C.c_uint32), ("flags", C.c_int32)]
        require((C.sizeof(Sigaction), C.alignment(Sigaction), Sigaction.mask.offset, Sigaction.flags.offset) == (16, 8, 8, 12), code)
        library = C.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        query = library.sigaction
        query.argtypes = [C.c_int, C.POINTER(Sigaction), C.POINTER(Sigaction)]
        query.restype = C.c_int
        old = Sigaction()
        require(query(int(signal.SIGCHLD), None, C.byref(old)) == 0, code)
        require(old.handler is None and not old.flags & 0x0020, code)
    except Blocked:
        raise
    except (OSError, AttributeError, TypeError, ValueError, C.ArgumentError) as error:
        raise Blocked(code) from error


def atomic_json(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


class RunLock:
    def __init__(self, path):
        self.path = Path(path)

    def __enter__(self):
        self.owner_pid = os.getpid()
        self.lock_identity = str(self.path)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            raise Blocked("another foreground run holds the lock")
        self.acquired = self.acquired_monotonic = time.monotonic()
        return self

    def __exit__(self, *args):
        os.close(self.fd)


class Supervisor:
    def __init__(self, run, *, seconds, log_limit=LOG_LIMIT, poll=1.0, grace=3.0, started=None, log_name="logs"):
        require(type(seconds) in (int, float) and math.isfinite(seconds) and seconds > 0, "explicit_process_cap_required")
        self.run = Path(run)
        require(log_name in ("logs", "design-logs"), "invalid_process_log_root")
        self.logs = self.run / log_name
        self.logs.mkdir()
        self.started = time.monotonic() if started is None else started
        self.deadline = self.started + seconds
        self.log_limit, self.poll, self.grace = log_limit, poll, grace
        self.sequence = 0
        self.unknown = False
        self.records = []
        self.on_record = None

    def alive(self, group):
        try:
            os.killpg(group, 0)
            return True
        except ProcessLookupError:
            return False
        except OSError:
            return True  # Unconfirmed existence/permission is never proof of exit.

    def sample_descendants(self, process, tracked):
        descendants=[]
        try:
            root=psutil.Process(process.pid)
            descendants=[root]+root.children(recursive=True)
        except psutil.NoSuchProcess:
            pass
        # A short-lived shell may orphan a new process group between polls.
        # Session membership survives reparenting; retain every observed group
        # and session as well as the root session created by start_new_session.
        groups={process.pid}|{item['pgid'] for item in tracked.values()}
        sessions={process.pid}|{item['sid'] for item in tracked.values()}
        for child in psutil.process_iter():
            if child.pid == 0:
                continue
            try:
                if os.getsid(child.pid) in sessions or os.getpgid(child.pid) in groups:
                    descendants.append(child)
            except (psutil.NoSuchProcess,ProcessLookupError):
                pass
        for child in descendants:
            try:
                observed_alive=tracked.get(child.pid,{}).get('observed_alive',False) or child.status()!=psutil.STATUS_ZOMBIE
                tracked[child.pid]={'pid':child.pid,'ppid':child.ppid(),'pgid':os.getpgid(child.pid),
                                    'sid':os.getsid(child.pid),'created':child.create_time(),'observed_alive':observed_alive}
            except (psutil.NoSuchProcess,ProcessLookupError):
                pass

    def running_descendants(self, tracked):
        running=[]
        for pid,identity in tracked.items():
            try:
                child=psutil.Process(pid)
                if child.create_time()==identity['created'] and child.is_running() and child.status()!=psutil.STATUS_ZOMBIE:
                    running.append(child)
            except psutil.NoSuchProcess:
                pass
        return running

    def end_group(self, process, tracked=None, *, cleanup_policy=None):
        tracked={} if tracked is None else tracked
        enumeration_complete=True
        errors=[]
        def sample():
            nonlocal enumeration_complete
            try:
                self.sample_descendants(process,tracked)
            except (psutil.Error,OSError) as error:
                enumeration_complete=False
                errors.append(type(error).__name__+': '+str(error))
        def running():
            nonlocal enumeration_complete
            children=[]
            for pid,identity in list(tracked.items()):
                try:
                    children.extend(self.running_descendants({pid:identity}))
                except (psutil.Error,OSError) as error:
                    enumeration_complete=False
                    errors.append(type(error).__name__+': '+str(error))
            return children
        def outcome():
            remaining=running()
            return {'known_ended':enumeration_complete and not self.alive(process.pid) and not remaining,
                    'enumeration_complete':enumeration_complete,'errors':errors}
        def signal_owned(sig):
            # Project cleanup never signals a recycled numeric group identity.
            # Each survivor must still match a previously observed creation time.
            # Pinned CPython Popen owns/reaps this direct child. Hold its
            # waitpid lock across the unreaped check and raw signal: calling
            # Popen.send_signal would poll/reap before the signal instead.
            wait_lock = process._waitpid_lock
            if wait_lock.acquire(False):
                try:
                    if process.returncode is None:
                        try:
                            os.kill(process.pid, sig)
                        except ProcessLookupError:
                            pass
                        except OSError as error:
                            errors.append(type(error).__name__ + ": " + str(error))
                finally:
                    wait_lock.release()
            else:
                errors.append("direct child wait ownership unavailable")
            owned = running()
            group_verified = False
            for child in owned:
                try:
                    identity = tracked[child.pid]
                    if child.create_time() == identity["created"] and os.getpgid(child.pid) == identity["pgid"] == process.pid:
                        group_verified = True
                except (psutil.Error, OSError) as error:
                    errors.append(type(error).__name__ + ": " + str(error))
            if group_verified:
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    pass
                except OSError as error:
                    errors.append(type(error).__name__ + ": " + str(error))
            for child in owned:
                try:
                    if child.create_time() == tracked[child.pid]["created"]:
                        child.send_signal(sig)
                except psutil.NoSuchProcess:
                    pass
                except (psutil.Error, OSError) as error:
                    errors.append(type(error).__name__ + ": " + str(error))
        cleanup_started = time.monotonic()
        if cleanup_policy is not None:
            require(set(cleanup_policy) == {"cleanup_seconds", "term_grace_seconds", "parent_deadline", "owner_deadline", "owner_hard_stop_utc"}, "cleanup_policy_shape")
            total, term = cleanup_policy["cleanup_seconds"], cleanup_policy["term_grace_seconds"]
            require(all(type(v) in (int, float) and math.isfinite(v) for v in (total, term, cleanup_policy["parent_deadline"], cleanup_policy["owner_deadline"])) and 0 <= term <= total and total > 0, "cleanup_policy_bounds")
            limit = min(cleanup_started + total, cleanup_policy["parent_deadline"], cleanup_policy["owner_deadline"])
            stop_utc = datetime.fromisoformat(cleanup_policy["owner_hard_stop_utc"].replace("Z", "+00:00"))
        else:
            limit, term, stop_utc = cleanup_started + 2 * self.grace, self.grace, None
        self.last_cleanup = {"cleanup_started_monotonic": cleanup_started, "cleanup_deadline_monotonic": limit}
        def remaining():
            return time.monotonic() < limit and (stop_utc is None or datetime.now(timezone.utc) < stop_utc)
        if cleanup_policy is not None and not remaining():
            # No renewed grace at a hard boundary: TERM then KILL, one final
            # nonblocking enumeration. Existing tracked identities gate kills.
            for sig in (signal.SIGTERM, signal.SIGKILL):
                signal_owned(sig)
            process.poll()
            sample()
            return outcome()
        for sig in (signal.SIGTERM, signal.SIGKILL):
            sample()
            if cleanup_policy is not None:
                signal_owned(sig)
            else:
                # Always signal the known group even if process enumeration failed.
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    pass
                except OSError as error:
                    errors.append(type(error).__name__+': '+str(error))
                for child in running():
                    try:
                        child.send_signal(sig)
                    except psutil.NoSuchProcess:
                        pass
                    except (psutil.Error,OSError) as error:
                        errors.append(type(error).__name__+': '+str(error))
            end = min(limit, cleanup_started + term) if sig == signal.SIGTERM else limit
            while time.monotonic() < end and remaining():
                process.poll()
                sample()
                if not self.alive(process.pid) and not running():
                    return outcome()
                wait = min(self.poll, 0.05, max(0, end - time.monotonic()))
                if stop_utc is not None:
                    wait = min(wait, max(0, (stop_utc - datetime.now(timezone.utc)).total_seconds()))
                if wait > 0:
                    time.sleep(wait)
        return outcome()

    def call(self, argv, *, cwd, env, stdin=None, success=True, model_role=None, stage="git", deadline=None,
             role_observer=None, cleanup_policy=None, control_after_observation=None):
        require(not self.unknown, "prior process termination unknown")
        require(control_after_observation is None or
                callable(control_after_observation) and role_observer is None,
                "invalid_control_observation_hook")
        if cleanup_policy is None:
            cleanup_policy = getattr(self, "cleanup_policy", None)
        # A role's transient cap belongs to this call, never to subsequent Git
        # capture/check/cleanup calls using the same foreground supervisor.
        effective_deadline = self.deadline if deadline is None else min(self.deadline, deadline)
        if cleanup_policy is not None:
            require(set(cleanup_policy) == {"cleanup_seconds", "term_grace_seconds", "parent_deadline", "owner_deadline", "owner_hard_stop_utc"}, "cleanup_policy_shape")
            require(all(type(cleanup_policy[k]) in (int, float) and math.isfinite(cleanup_policy[k]) for k in
                ("cleanup_seconds", "term_grace_seconds", "parent_deadline", "owner_deadline")) and
                0 <= cleanup_policy["term_grace_seconds"] <= cleanup_policy["cleanup_seconds"] and cleanup_policy["cleanup_seconds"] > 0, "cleanup_policy_bounds")
            effective_deadline = min(effective_deadline, cleanup_policy["parent_deadline"]-cleanup_policy["cleanup_seconds"],
                                     cleanup_policy["owner_deadline"]-cleanup_policy["cleanup_seconds"])
            owner_stop = datetime.fromisoformat(cleanup_policy["owner_hard_stop_utc"].replace("Z", "+00:00"))
            require(datetime.now(timezone.utc) < owner_stop-timedelta(seconds=cleanup_policy["cleanup_seconds"]), "owner_productive_stop")
        require(time.monotonic() < effective_deadline, "whole-run deadline exceeded")
        if cleanup_policy is not None:
            require_reaping_custody()
            # Query/source IO consumes the original allowance; no reset/grace.
            require(time.monotonic() < effective_deadline, "whole-run deadline exceeded")
            require(datetime.now(timezone.utc) < owner_stop-timedelta(seconds=cleanup_policy["cleanup_seconds"]), "owner_productive_stop")
        self.sequence += 1
        prefix = self.logs / f"{self.sequence:04d}"
        out, err = prefix.with_suffix(".stdout"), prefix.with_suffix(".stderr")
        record = {"argv": [str(a) for a in argv], "cwd": str(cwd), "stdout": str(out), "stderr": str(err), "started": time.time(), "known_ended": False,
                  "observation_scope": "Direct child, sampled descendants and observed process groups; an unobserved escaped descendant is not excluded.",
                  "launch_status": "not_started", "owner_pid": os.getpid()}
        record.update(stage=stage, started_monotonic=time.monotonic(), deadline_monotonic=effective_deadline,
                      supervisor_started_monotonic=self.started, supervisor_deadline_monotonic=self.deadline)
        if model_role is not None:
            record['model_role']=model_role
        process = None
        failure = None
        tracked={}
        tracking_complete=True
        observed_offset = 0
        def check_productive():
            if time.monotonic() >= effective_deadline:
                raise Blocked("whole-run timeout")
            if cleanup_policy is not None:
                owner_stop = datetime.fromisoformat(cleanup_policy["owner_hard_stop_utc"].replace("Z", "+00:00"))
                require(datetime.now(timezone.utc) < owner_stop-timedelta(seconds=cleanup_policy["cleanup_seconds"]), "owner_productive_stop")
        def finalization_failed(stage, error):
            nonlocal failure
            self.unknown = True
            record["known_ended"] = False
            record.setdefault("finalization_errors", []).append({"stage": stage, "type": type(error).__name__, "message": str(error)})
            if failure is None:
                failure = error
                record["failure"] = type(error).__name__ + ": " + str(error)
        try:
            with out.open("xb") as stdout, err.open("xb") as stderr:
                record['launch_status']='unknown'
                process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=stdin if stdin is not None else subprocess.DEVNULL,
                                           stdout=stdout, stderr=stderr, start_new_session=True)
                record["pid"] = process.pid
                record['launch_status']='started'
                while True:
                    check_productive()
                    try:
                        self.sample_descendants(process,tracked)
                    except (psutil.Error,OSError):
                        tracking_complete=False
                        raise Blocked('process descendant enumeration unavailable')
                    check_productive()
                    rc = process.poll()
                    check_productive()
                    size = max(out.stat().st_size, err.stat().st_size)
                    check_productive()
                    if size > self.log_limit:
                        raise Blocked("process stream overflow")
                    if role_observer is not None:
                        with out.open("rb") as observed:
                            observed.seek(observed_offset)
                            chunk = observed.read(65536)
                        check_productive()
                        if chunk:
                            stop = role_observer.feed(chunk, observed_offset, time.monotonic())
                            check_productive()
                            observed_offset += len(chunk)
                            if stop is not None:
                                record["stop_observation"] = stop
                                raise Blocked(stop["reason"])
                    if rc is None and control_after_observation is not None:
                        check_productive()
                        identity = tracked.get(process.pid)
                        require(type(identity) is dict and type(process.pid) is int and process.pid > 0 and
                                identity.get("pid") == process.pid and identity.get("ppid") == os.getpid() and
                                identity.get("pgid") == identity.get("sid") == process.pid and
                                identity.get("observed_alive") is True and
                                type(identity.get("created")) in (int, float) and
                                math.isfinite(identity["created"]) and identity["created"] > 0,
                                "control_inner_identity_unavailable")
                        control_after_observation()
                        check_productive()
                    if rc is not None:
                        if self.alive(process.pid) or self.running_descendants(tracked):
                            raise Blocked("child exited with surviving process group or descendant")
                        break
                    try:
                        wait = min(self.poll, max(0, effective_deadline - time.monotonic()))
                        if cleanup_policy is not None:
                            wait = min(wait, max(0, (owner_stop-timedelta(seconds=cleanup_policy["cleanup_seconds"]) - datetime.now(timezone.utc)).total_seconds()))
                        process.wait(timeout=wait)
                    except subprocess.TimeoutExpired:
                        pass
                record.update(exit_code=rc, known_ended=True)
        except BaseException as exc:
            failure = exc
            if process is not None:
                self.unknown = True
                try:
                    cleanup = self.end_group(process,tracked,cleanup_policy=cleanup_policy)
                    record.update(getattr(self, "last_cleanup", {}))
                    tracking_complete = tracking_complete and cleanup['enumeration_complete']
                    record["known_ended"] = cleanup['known_ended'] and tracking_complete
                    if cleanup['errors']:
                        record['termination_observation_errors']=cleanup['errors']
                    record["exit_code"] = process.poll()
                    self.unknown = not record["known_ended"]
                except BaseException as termination_error:
                    record["termination_error"] = type(termination_error).__name__ + ': ' + str(termination_error)
                    record['exit_code']=process.poll()
            elif isinstance(exc, OSError):
                # Popen reaps an exec-failed child before raising OSError.
                record['known_ended'] = True
                record['launch_status']='not_started'
            else:
                # Popen may be interrupted after fork but before returning a
                # process handle. No handle is not proof that no child exists.
                self.unknown = True
            record["failure"] = type(exc).__name__ + ": " + str(exc)
        finally:
            record['observed_process_tree']=list(tracked.values())
            record['descendant_enumeration_complete']=tracking_complete
            record['root_observed_alive']=process is not None and tracked.get(process.pid,{}).get('observed_alive',False)
            record["ended"] = time.time()
            record["ended_monotonic"] = time.monotonic()
            self.records.append(record)
            try:
                atomic_json(prefix.with_suffix(".json"), record)
            except BaseException as error:
                finalization_failed("publication", error)
            else:
                if self.on_record is not None:
                    try:
                        self.on_record(record)
                    except BaseException as error:
                        finalization_failed("on_record", error)
        if failure is not None:
            raise failure
        require(not success or record["exit_code"] == 0, "child returned nonzero; see " + str(prefix))
        return record
