"""Windows DPAPI MCP launcher with an operator-pinned, private profile.

Run using the selected runtime as: python -I -S -B windows-pinned-mcp.py
--profile ABSOLUTE_PATH --profile-sha256 EXPECTED_SHA256. All paths in the
profile are relative to its absolute private root. The profile and manifest
are local integrity checks, not signatures or a same-user security boundary.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

SHA = re.compile(r'[0-9a-f]{64}\Z')
ENV_ALLOW = ('SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'COMSPEC',
             'PATHEXT', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate field')
        result[key] = value
    return result


def no_reparse(path):
    path = Path(os.path.abspath(path))
    for part in reversed((path, *path.parents)):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('reparse point')
    return path


def private_path(root, relative, *, directory=False):
    if not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative:
        raise ValueError('invalid relative path')
    parts = relative.split('/')
    if any(p in ('', '.', '..') or p.endswith(('.', ' ')) for p in parts):
        raise ValueError('invalid relative path')
    path = no_reparse(root.joinpath(*parts))
    info = path.lstat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError('invalid file type')
    return path


def read_json(path, limit):
    raw = path.read_bytes()
    if len(raw) > limit:
        raise ValueError('file too large')
    return raw, json.loads(raw, object_pairs_hook=unique_object)


def verify_tree(site, expected):
    if not isinstance(expected, dict) or not expected:
        raise ValueError('invalid manifest')
    base = private_path(site, 'aios', directory=True)
    actual = {}
    for parent, dirs, files in os.walk(base, followlinks=False):
        parent = Path(parent)
        for name in dirs:
            if name == '__pycache__':
                raise ValueError('unexpected bytecode')
            no_reparse(parent / name)
        for name in files:
            entry = no_reparse(parent / name)
            info = entry.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_size > 16 * 1024 * 1024 or
                    entry.suffix.casefold() == '.pyc' or len(actual) >= 10000):
                raise ValueError('unexpected tree entry')
            relative = entry.relative_to(site).as_posix().casefold()
            if relative in actual:
                raise ValueError('case collision')
            actual[relative] = digest(entry.read_bytes())
    normalized = {key.casefold(): value for key, value in expected.items()
                  if isinstance(key, str) and isinstance(value, str) and SHA.fullmatch(value)}
    if len(normalized) != len(expected) or actual != normalized:
        raise ValueError('tree mismatch')


class Blob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def decrypt_dpapi(encoded):
    encrypted = bytes.fromhex(encoded.strip())
    if not encrypted or len(encrypted) > 16384:
        raise ValueError('invalid credential')
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt.CryptUnprotectData.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buf = (ctypes.c_ubyte * len(encrypted)).from_buffer_copy(encrypted)
    source, output = Blob(len(encrypted), buf), Blob()
    if not crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ValueError('credential unavailable')
    try:
        return ctypes.string_at(output.pbData, output.cbData).decode('utf-16-le')
    finally:
        ctypes.memset(output.pbData, 0, output.cbData)
        kernel.LocalFree(output.pbData)


def prepare(profile_file, expected_profile):
    if not SHA.fullmatch(expected_profile):
        raise ValueError('invalid profile pin')
    raw, profile = read_json(no_reparse(profile_file), 262144)
    if digest(raw) != expected_profile or not isinstance(profile, dict):
        raise ValueError('profile mismatch')
    required = {'root', 'runtime', 'runtime_sha256', 'config_file', 'ca_file', 'secret_file', 'package_root',
                'manifest_file', 'manifest_sha256', 'ca_sha256', 'config', 'module', 'distributions'}
    if set(profile) != required or profile['module'] not in ('aios', 'aios.memory_mcp'):
        raise ValueError('invalid profile')
    root = Path(profile['root'])
    if not root.is_absolute() or not stat.S_ISDIR(no_reparse(root).lstat().st_mode):
        raise ValueError('invalid root')
    runtime = private_path(root, profile['runtime'])
    if (not isinstance(profile['runtime_sha256'], str) or not SHA.fullmatch(profile['runtime_sha256']) or
            digest(runtime.read_bytes()) != profile['runtime_sha256']):
        raise ValueError('runtime mismatch')
    config = private_path(root, profile['config_file'])
    ca = private_path(root, profile['ca_file'])
    secret = private_path(root, profile['secret_file'])
    site = private_path(root, profile['package_root'], directory=True)
    manifest = private_path(root, profile['manifest_file'])
    if not isinstance(profile['ca_sha256'], str) or not SHA.fullmatch(profile['ca_sha256']) or digest(ca.read_bytes()) != profile['ca_sha256']:
        raise ValueError('CA mismatch')
    raw_manifest, expected = read_json(manifest, 2097152)
    if not isinstance(profile['manifest_sha256'], str) or not SHA.fullmatch(profile['manifest_sha256']) or digest(raw_manifest) != profile['manifest_sha256']:
        raise ValueError('manifest mismatch')
    verify_tree(site, expected)
    _, cfg = read_json(config, 262144)
    pinned = profile['config']
    if (not isinstance(cfg, dict) or not isinstance(pinned, dict) or
            set(pinned) != {'mode', 'instance_id', 'endpoint', 'token_env'} or
            pinned.get('mode') != 'client' or not isinstance(pinned.get('endpoint'), str) or
            not pinned['endpoint'].startswith('https://') or
            not isinstance(pinned.get('token_env'), str) or
            not re.fullmatch(r'SIOS_[A-Z0-9_]*TOKEN', pinned['token_env']) or
            any(cfg.get(k) != v for k, v in pinned.items())):
        raise ValueError('configuration mismatch')
    if os.path.normcase(os.path.abspath(sys.executable)) != os.path.normcase(os.path.abspath(runtime)):
        raise ValueError('runtime mismatch')
    versions = profile['distributions']
    if (not isinstance(versions, dict) or not versions or len(versions) > 16 or
            not {'mcp', 'pydantic'}.issubset(versions) or
            any(not isinstance(k, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', k) or
                not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}', v)
                for k, v in versions.items())):
        raise ValueError('invalid versions')
    return runtime, config, ca, secret, site, pinned['token_env'], profile['module'], versions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--profile-sha256', required=True)
    args = parser.parse_args()
    if os.name != 'nt' or not args.profile.is_absolute() or not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise ValueError('invalid entry')
    runtime, config, ca, secret, site, token_env, module, versions = prepare(args.profile, args.profile_sha256)
    env = {key: os.environ[key] for key in ENV_ALLOW if key in os.environ}
    check = 'import importlib.metadata as m; assert all(m.version(k)==v for k,v in ' + repr(versions) + '.items())'
    verified = subprocess.run([str(runtime), '-I', '-B', '-c', check], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, timeout=15)
    if verified.returncode:
        raise ValueError('runtime version mismatch')
    token = decrypt_dpapi(secret.read_text(encoding='ascii'))
    if not 32 <= len(token) <= 4096 or not all(33 <= ord(c) <= 126 for c in token):
        raise ValueError('invalid credential')
    env[token_env] = token
    env['PYTHONPATH'] = str(site)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['PYTHONNOUSERSITE'] = '1'
    try:
        return subprocess.Popen([str(runtime), '-P', '-B', '-m', module, '--config', str(config),
                                 '--ca-file', str(ca), 'mcp'], env=env).wait()
    finally:
        env.pop(token_env, None)
        token = None


if __name__ == '__main__':
    try:
        status = main()
    except Exception:
        print('sios_pinned_launcher_failed', file=sys.stderr, flush=True)
        status = 1
    raise SystemExit(status)
