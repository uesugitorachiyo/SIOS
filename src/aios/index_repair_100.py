"""Unregistered Mac candidate for a bounded BM 1.0 single-file repair."""
import fcntl
import hashlib
import json
import os
import signal
from pathlib import Path
import sqlite3
import stat
import subprocess
from functools import partial

PROGRAM = '''import asyncio,sys\nfrom basic_memory.mcp.async_client import _asgi_client,_build_timeout\nasync def run():\n async with _asgi_client(_build_timeout()) as c:\n  r=await c.post('/v2/projects/'+sys.argv[1]+'/knowledge/index-file',json={'file_path':sys.argv[2]})\n  if r.status_code!=200: raise SystemExit(1)\nasyncio.run(run())\n'''


def from_environment(environment):
    """Construct an explicitly enabled candidate; launcher owns these paths."""
    if environment.get('SIOS_REPAIR_100_ENABLED') != '1':
        return None
    paths=[]
    for name in ('SIOS_REPAIR_PYTHON','SIOS_REPAIR_VENDOR','SIOS_REPAIR_CACHE'):
        value=environment.get(name,'')
        path=Path(value)
        if not value or not path.is_absolute() or not path.exists():
            raise ValueError('invalid repair runtime')
        paths.append(path)
    if not paths[0].is_file() or not paths[1].is_dir() or not paths[2].is_dir():
        raise ValueError('invalid repair runtime type')
    return partial(repair,python=paths[0],vendor=paths[1],cache=paths[2])


def _binding_snapshot(index):
    binding=index.memory_root.parent/'.aios-basic-memory.json'
    config=index.config_dir/'config.json'
    db=index.config_dir/'memory.db'
    def identity(path):
        node=path.lstat()
        if not stat.S_ISREG(node.st_mode): raise ValueError('nonregular binding')
        return node.st_dev,node.st_ino
    # Content changes in the configuration matter; database data naturally
    # changes during repair, so only its file identity is compared.
    return (identity(binding),hashlib.sha256(binding.read_bytes()).digest(),
            identity(config),hashlib.sha256(config.read_bytes()).digest(),identity(db))


def repair(index, *, rel_path, expected_revision, python, vendor, cache, timeout=35, store=None, marker_token=None):
    """Return settled only after the direct child exited successfully.

    Caller must have recorded a durable uncertainty marker before calling.
    This candidate does not establish that detached descendants cannot exist.
    """
    index._validate_configuration()
    binding_before=_binding_snapshot(index)
    source=index.memory_root / rel_path
    if source.is_symlink() or hashlib.sha256(source.read_bytes()).hexdigest()!=expected_revision:
        raise ValueError('source changed')
    database=index.config_dir/'memory.db'
    with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as db:
        rows=db.execute('SELECT external_id,name,path FROM project').fetchall()
    if len(rows)!=1 or rows[0][1]!=index.project or Path(rows[0][2]).resolve()!=index.memory_root.resolve():
        raise ValueError('project mismatch')
    lock_path=index.config_dir.parent/'sios-repair-child.lock'
    fd=os.open(lock_path,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        info=os.fstat(fd); named=os.stat(lock_path,follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or (info.st_dev,info.st_ino)!=(named.st_dev,named.st_ino):
            raise ValueError('lock identity mismatch')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        promoted = None
        if store is not None and marker_token is not None:
            from .index_retry import initialize
            database_id=database.stat()
            suffix=marker_token.removeprefix('unknown1-')
            if marker_token!='unknown1-'+suffix or len(suffix)!=32:
                raise ValueError('unexpected marker')
            promoted=f'nofork2-{info.st_dev}-{info.st_ino}-{database_id.st_dev}-{database_id.st_ino}-{suffix}'
            with store._connection() as db:
                initialize(db)
                n=db.execute('UPDATE index_repair_uncertainty SET token=? WHERE path=? AND revision=? AND token=?',
                             (promoted,'Memory/'+rel_path,expected_revision,marker_token)).rowcount
            if n!=1: raise ValueError('marker changed')
        env=index._environment()
        env.update({'PYTHONPATH':str(vendor),'FASTEMBED_CACHE_PATH':str(cache),
                    'BASIC_MEMORY_SEMANTIC_EMBEDDING_MODEL':'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2',
                    'HF_HUB_OFFLINE':'1','PYTHONDONTWRITEBYTECODE':'1'})
        profile='(version 1) (allow default) (deny network*) (deny process-fork) (deny file-write* (subpath '+json.dumps(str(index.memory_root))+'))'
        child=subprocess.Popen(['/usr/bin/sandbox-exec','-p',profile,str(python),'-c',PROGRAM,rows[0][0],rel_path],
                               env=env,cwd=index.config_dir.parent,stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,pass_fds=(fd,),start_new_session=True)
        try:
            code=child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # This group belongs to the child created above. Reap the direct
            # child, but retain the durable marker: effects remain unknown.
            try:
                os.killpg(child.pid,signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            raise TimeoutError('repair still unconfirmed') from None
        if code!=0:
            raise RuntimeError('repair failed or unconfirmed')
        named=os.stat(lock_path,follow_symlinks=False)
        if (info.st_dev,info.st_ino)!=(named.st_dev,named.st_ino):
            raise ValueError('lock replaced')
        index._validate_configuration()
        if _binding_snapshot(index)!=binding_before:
            raise ValueError('index binding changed')
        if hashlib.sha256(source.read_bytes()).hexdigest()!=expected_revision:
            raise ValueError('source changed')
        return {'settled':True,'marker_token':promoted} if promoted else {'settled':True}
    finally:
        os.close(fd)
