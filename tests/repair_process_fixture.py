"""Fictional process-lifetime experiment, not a production repair runner."""
import fcntl
import os
from pathlib import Path
import stat
import subprocess


def open_lock(path):
    fd=os.open(path,os.O_CREAT | os.O_RDWR | getattr(os,'O_NOFOLLOW',0),0o600)
    info=os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise ValueError('not regular')
    return fd


def launch_with_lock(lock_path, command):
    fd=open_lock(lock_path)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        # Keep the same open file description in the direct child even if the
        # supervisor dies. This says nothing about detached descendants.
        child=subprocess.Popen(command,pass_fds=(fd,),close_fds=True)
        return fd,child
    except BaseException:
        os.close(fd)
        raise


def available(lock_path):
    fd=open_lock(lock_path)
    try:
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    finally:
        os.close(fd)
