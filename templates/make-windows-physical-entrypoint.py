"""Create an optional SIOS Windows entrypoint bound to one physical venv.

Run with that venv's Python after installing sios-core. This uses pip's
bundled distlib, so pip must already be present in the same runtime.
No client config, credential, or service state is read or changed.
"""

import argparse
import os
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", type=Path, help="absolute path to the installed venv")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("this entrypoint generator runs on Windows only")
    if not args.runtime.is_absolute():
        parser.error("runtime must be an absolute path")

    runtime = args.runtime.resolve(strict=True)
    scripts = runtime / "Scripts"
    python = scripts / "python.exe"
    target = scripts / "sios-physical.exe"
    if not python.is_file():
        parser.error("runtime Python is missing")
    if target.exists():
        parser.error("sios-physical.exe already exists; inspect it before replacement")

    from pip._vendor.distlib.scripts import ScriptMaker

    maker = ScriptMaker(None, str(scripts))
    maker.executable = str(python)
    maker.variants = {""}
    maker.clobber = False
    maker.make("sios-physical = aios.cli:main")
    if not target.is_file():
        raise RuntimeError("physical SIOS entrypoint was not created")
    print(str(target))


if __name__ == "__main__":
    main()
