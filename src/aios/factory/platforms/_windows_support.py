"""Bounded Windows path, handle, and evidence helpers."""
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import sys
from ..contract import Blocked, plain, require
LOG_LIMIT = 20 * 1024 * 1024

def _kernel():
    require(sys.platform == "win32", "windows_clock_unsupported_abi")
    # Fixed System32/KnownDLL trust. No task-controlled DLL or search path.
    return C.WinDLL("kernel32.dll", use_last_error=True, winmode=0x800)

def _identity_error(code, stage, *, winerror=None, changed=()):
    # Closed stage/field names only. No paths, token facts, bytes or FFI text.
    error = Blocked(code)
    error.identity_stage = stage
    error.identity_changed_fields = tuple(changed)
    if type(winerror) is int:
        error.winerror = winerror
    error.args = (code + " [stage=" + stage +
                  (";changed=" + ",".join(changed) if changed else "") + "]",)
    return error

def _close_identity_handle(close, handle, primary, code, attribute):
    """One owned handle, no retry; preserve the actual first failure/interrupt."""
    secondary = None
    try:
        if close(handle) == 0:
            secondary = _identity_error(code, "close", winerror=C.get_last_error())
    except BaseException as error:
        secondary = error
    if secondary is None:
        return
    if primary is not None:
        setattr(primary, attribute, True)
        # Fixed evidence only; never format the secondary FFI exception.
        primary.identity_close_failure = "close_unknown"
        return
    if not isinstance(secondary, Exception):
        setattr(secondary, attribute, True)
        raise secondary
    error = _identity_error(code, "close", winerror=getattr(secondary, "winerror", None))
    setattr(error, attribute, True)
    raise error from secondary

def plain_path(path):
    return plain(Path(path), directory=Path(path).is_dir())

def digest(path):
    """c5 member-image sampler only: file-at-path bytes, not tool admission.

    Selected executables/source/config still require plain/single-link checks.
    This sampled helper path has no mapped-image identity or bounded-IO promise.
    """
    value=hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda:stream.read(1024*1024),b""):
            value.update(block)
    return value.hexdigest()

def handle_identity(handle):
    require(handle is not None, "missing_foreground_lease")
    k = _kernel()
    info = k.GetFileInformationByHandle
    info.argtypes, info.restype = [C.c_void_p, C.c_void_p], C.c_int32
    raw = (C.c_uint32 * 13)()
    require(info(handle, C.byref(raw)) != 0 and raw[10] == 1 and not raw[0] & 0x400, "missing_foreground_lease")
    return raw[7], raw[11], raw[12]

def directory_identity(path):
    path=plain(Path(path),directory=True);k=_kernel()
    create=k.CreateFileW
    create.argtypes,create.restype=[C.c_wchar_p,C.c_uint32,C.c_uint32,C.c_void_p,C.c_uint32,C.c_uint32,C.c_void_p],C.c_void_p
    close=k.CloseHandle;close.argtypes,close.restype=[C.c_void_p],C.c_int32
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    handle=create(str(path),0x80,7,None,3,FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,None)
    if handle in (None,C.c_void_p(-1).value):
        winerror = C.get_last_error()
        error = Blocked("windows_directory_identity")
        error.winerror = winerror
        raise error
    primary = None
    try:
        return handle_identity(handle)
    except BaseException as error:
        primary = error
        raise
    finally:
        _close_identity_handle(close, handle, primary, "windows_directory_identity", "directory_handle_close_unknown")

def publish_native_json(path,value,owned_directory_identity):
    """Native05 provisional snapshot: owned directory, exclusive stage, no replace."""
    require(sys.platform=="win32","native_publication_host")
    path=Path(path);require(directory_identity(path.parent)==owned_directory_identity,"native_publication_directory_identity")
    temporary=path.with_name(path.name+".partial")
    require(not path.exists() and not path.is_symlink(),"native_publication_exists")
    data=json.dumps(value,indent=2,ensure_ascii=True,allow_nan=False).encode("utf-8")+b"\n"
    with temporary.open("xb") as stream:
        require(os.fstat(stream.fileno()).st_nlink==1,"native_publication_alias")
        stream.write(data);stream.flush();os.fsync(stream.fileno())
    plain(temporary)
    require(temporary.stat().st_nlink==1 and temporary.stat().st_size==len(data) and
            directory_identity(path.parent)==owned_directory_identity and not path.exists(),"native_publication_alias")
    os.rename(temporary,path)  # Windows no-replace; no retry or alternative primitive.

def validate_native_path(path):
    """The admitted local-drive subset; never enables device/UNC/long paths."""
    import ntpath
    import re
    from pathlib import PureWindowsPath
    raw=str(path)
    try:units=len(raw.encode('utf-16-le',errors='strict'))//2
    except UnicodeError as exc:raise Blocked('windows_path_budget') from exc
    drive,tail=ntpath.splitdrive(raw)
    require('\0' not in raw and units+1<=260 and not raw.startswith(('\\\\','\\\\?\\','\\\\.\\')) and
            re.fullmatch('[A-Za-z]:',drive) and tail.startswith(('\\','/')),'windows_path_budget')
    for part in PureWindowsPath(raw).parts[1:]:
        require(part not in ('.','..') and part==part.rstrip(' .') and ':' not in part and
                not any(ord(c)<32 or c in '<>"|?*' for c in part) and
                not re.fullmatch(r'(?:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?',part,re.I),'windows_path_policy')
    return path
