"""Mount an existing SIOS Memory directory in the current Windows session.

This program does not copy or inspect Memory files. The private configuration
and the already-verified SSH host and key are maintained outside the checkout.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import time


def read_config(path: Path) -> tuple[str, str, str]:
    if path.stat().st_size > 8192:
        raise ValueError("Configuration is too large")
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or set(config) != {"host_alias", "remote_path", "drive"}:
        raise ValueError("Configuration must contain host_alias, remote_path, and drive")
    alias, remote, drive = (config[k] for k in ("host_alias", "remote_path", "drive"))
    if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", alias):
        raise ValueError("Invalid SSH host alias")
    if not isinstance(remote, str) or not remote.startswith("/") or any(
        ord(c) < 32 or c in "\\:" for c in remote
    ) or any(part in {".", ".."} for part in remote.split("/")) or str(PurePosixPath(remote)) != remote:
        raise ValueError("Invalid absolute remote path")
    if not isinstance(drive, str) or not re.fullmatch(r"[D-Z]:", drive):
        raise ValueError("Drive must be an uppercase letter from D: to Z:")
    return alias, remote, drive


def write_status(path: Path, state: str) -> None:
    # Status contains no file names or contents. Never let a status write stop a mount.
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"state": state}, ensure_ascii=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This launcher requires Windows")
    try:
        alias, remote, drive = read_config(args.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        return 2
    status = args.config.parent / "status.json"
    exe = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "SSHFS-Win" / "bin" / "sshfs-win.exe"
    if not exe.is_file():
        write_status(status, "sshfs_not_installed")
        return 2
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel.GetLogicalDrives.restype = ctypes.c_uint32
    mutex = kernel.CreateMutexW(None, False, "Local\\SIOSMemoryDrive_" + drive[0])
    if not mutex:
        write_status(status, "mutex_error")
        return 2
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(mutex)
        return 0
    command = [
        str(exe), "cmd", f"{alias}:{remote}", drive, "-f",
        "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes",
        "-o", "reconnect", "-o", "dir_cache=no", "-o", "idmap=user",
        "-o", "uid=-1,gid=-1", "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
    ]
    bit = 1 << (ord(drive[0]) - ord("A"))
    try:
        while True:
            if kernel.GetLogicalDrives() & bit:
                write_status(status, "drive_in_use")
                return 1
            write_status(status, "connecting")
            try:
                process = subprocess.Popen(
                    command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except OSError:
                write_status(status, "launch_failed")
                time.sleep(20)
                continue
            deadline = time.monotonic() + 20
            while process.poll() is None and time.monotonic() < deadline:
                if kernel.GetLogicalDrives() & bit:
                    write_status(status, "connected")
                    process.wait()
                    break
                time.sleep(0.25)
            else:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
            write_status(status, "disconnected_retrying")
            time.sleep(20)
    finally:
        kernel.CloseHandle(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
