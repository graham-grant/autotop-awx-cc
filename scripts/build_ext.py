#!/usr/bin/env python3
"""
Builds the required C extension (csrc/_hanan.c -> scripts/_hanan*.so)
using only the standard library: no setuptools, no pip. It asks sysconfig
for the compiler and flags CPython itself was built with, so the result
matches the running interpreter's ABI.

Needs a C compiler and the Python development headers (Python.h) on the
machine that runs it - in an AWX execution environment that means gcc and
python3-devel (or python3-dev) in the image. main.py calls build() on
first run if the module is missing or older than its source.

Usage: python3 build_ext.py [--force]
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "csrc" / "_hanan.c"


class BuildError(RuntimeError):
    pass


def target_path() -> Path:
    return HERE / ("_hanan" + sysconfig.get_config_var("EXT_SUFFIX"))


def is_stale() -> bool:
    target = target_path()
    return not target.exists() or target.stat().st_mtime < SOURCE.stat().st_mtime


def _command(target: Path) -> list[str]:
    cc = os.environ.get("CC") or sysconfig.get_config_var("CC") or "cc"
    include = sysconfig.get_paths()["include"]
    if not (Path(include) / "Python.h").exists():
        raise BuildError(f"Python.h not found in {include} - install the Python "
                         f"development headers (python3-devel / python3-dev)")
    cc_argv = shlex.split(cc)
    if not shutil.which(cc_argv[0]):
        raise BuildError(f"C compiler '{cc_argv[0]}' not found - install gcc")

    flags = ["-O2", "-fPIC", "-ffp-contract=off", "-Wall", f"-I{include}"]
    if sys.platform == "darwin":
        link = ["-bundle", "-undefined", "dynamic_lookup"]
    else:
        link = ["-shared"]
    return cc_argv + flags + [str(SOURCE), "-o", str(target)] + link + ["-lm"]


def build(force: bool = False, quiet: bool = False) -> Path:
    target = target_path()
    if not force and not is_stale():
        return target
    cmd = _command(target)
    if not quiet:
        print("Building C extension:", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise BuildError(f"compiling {SOURCE.name} failed:\n{proc.stderr}")
    if proc.stderr.strip() and not quiet:
        print(proc.stderr)
    return target


if __name__ == "__main__":
    try:
        print("Built", build(force="--force" in sys.argv[1:]))
    except BuildError as e:
        sys.exit(str(e))
