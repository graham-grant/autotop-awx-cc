#!/usr/bin/env python3
"""
Builds the required C extension csrc/_hanan.c using only the standard
library: no setuptools, no pip. It asks sysconfig for the compiler and
include path of the running interpreter.

The extension uses CPython's stable ABI, so the result -
scripts/_hanan.abi3.so - loads on every CPython 3.9+ for the same OS and
CPU architecture. A prebuilt Linux x86_64 copy is committed to the repo,
so AWX execution environments need neither a compiler nor Python headers.
Rebuild and commit it whenever csrc/_hanan.c changes (the test suite
fails if you forget):

    python3 scripts/build_ext.py

The compiled module only needs glibc symbols from 2.2.5/2.4 (the tests
check it stays at or below 2.17), so a copy built on a current distro loads
on any glibc-based Linux in use - RHEL/CentOS 7+, every AWX EE image.

The build stamps the source's SHA-256 into the module (__source_sha256__),
which pathfinder.py compares against the source at runtime.

Usage: python3 build_ext.py [--check] [--out PATH]
  --check     exit 0 if the committed binary matches the source, 1 if not
  --out PATH  write the module somewhere other than scripts/
"""
from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "csrc" / "_hanan.c"
TARGET = HERE / ("_hanan.abi3.so" if sys.platform != "win32" else "_hanan.pyd")


class BuildError(RuntimeError):
    pass


def source_sha256() -> str:
    return hashlib.sha256(SOURCE.read_bytes()).hexdigest()


def built_sha256(path: Path = TARGET) -> str | None:
    """The source hash stamped into a built module, read without importing
    it (the stamp is a plain string constant in the binary)."""
    if not path.exists():
        return None
    data = path.read_bytes()
    marker = b"HANAN_SRC_SHA256="
    i = data.find(marker)
    if i < 0:
        return None
    return data[i + len(marker):i + len(marker) + 64].decode("ascii", "replace")


def is_current(path: Path = TARGET) -> bool:
    return built_sha256(path) == source_sha256()


def _command(target: Path) -> list[str]:
    cc = os.environ.get("CC") or sysconfig.get_config_var("CC") or "cc"
    include = sysconfig.get_paths()["include"]
    if not (Path(include) / "Python.h").exists():
        raise BuildError(f"Python.h not found in {include} - install the Python "
                         f"development headers (python3-devel / python3-dev), or use the "
                         f"prebuilt scripts/{TARGET.name} committed to the repo")
    cc_argv = shlex.split(cc)
    if not shutil.which(cc_argv[0]):
        raise BuildError(f"C compiler '{cc_argv[0]}' not found - install gcc, or use the "
                         f"prebuilt scripts/{TARGET.name} committed to the repo")

    digest = source_sha256()
    flags = ["-O2", "-fPIC", "-ffp-contract=off", "-Wall", "-std=gnu99", f"-I{include}",
             f'-DHANAN_SOURCE_SHA256="{digest}"',
             f'-DHANAN_SOURCE_STAMP="HANAN_SRC_SHA256={digest}"']
    if sys.platform == "darwin":
        link = ["-bundle", "-undefined", "dynamic_lookup"]
    else:
        link = ["-shared"]
    return cc_argv + flags + [str(SOURCE), "-o", str(target)] + link + ["-lm"]


def build(target: Path = TARGET, quiet: bool = False) -> Path:
    """Compile to `target`, via a temp file + rename so a module that's
    already loaded from that path is never overwritten in place."""
    target = Path(target)
    tmp = target.with_name(target.name + ".tmp")
    cmd = _command(tmp)
    if not quiet:
        print("Building C extension:", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise BuildError(f"compiling {SOURCE.name} failed:\n{proc.stderr}")
    if proc.stderr.strip() and not quiet:
        print(proc.stderr)
    os.replace(tmp, target)
    return target


if __name__ == "__main__":
    args = sys.argv[1:]
    try:
        if "--check" in args:
            ok = is_current()
            print(f"{TARGET.name}: {'up to date' if ok else 'STALE or missing - rebuild it'}")
            sys.exit(0 if ok else 1)
        out = Path(args[args.index("--out") + 1]) if "--out" in args else TARGET
        print("Built", build(out))
    except BuildError as e:
        sys.exit(str(e))
