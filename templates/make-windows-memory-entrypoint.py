"""Generate a Windows entrypoint bound to its physical venv interpreter."""
from pathlib import Path
import argparse
import os
from pip._vendor.distlib.scripts import ScriptMaker
p=argparse.ArgumentParser()
p.add_argument("runtime",type=Path)
a=p.parse_args()
if os.name != 'nt': raise SystemExit('Windows only')
if not a.runtime.is_absolute(): raise SystemExit('runtime must be absolute')
runtime=a.runtime.resolve(strict=True)
python=runtime/"Scripts"/"python.exe"
if not python.is_file(): raise SystemExit("Missing runtime Python")
if (runtime/"Scripts"/"sios-memory.exe").exists(): raise SystemExit('sios-memory.exe already exists')
maker=ScriptMaker(None,str(runtime/"Scripts"))
maker.executable=str(python)
maker.variants={""}
maker.clobber=False
print("\n".join(maker.make("sios-memory = aios.memory_mcp:main")))
