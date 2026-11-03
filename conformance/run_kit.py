#!/usr/bin/env python3
"""
Runs a conformance kit (conformance/stage<N>/kit.json) against the code in
this repository and says whether every result matches the reference
implementation exactly.

A kit is data, not code: a list of vectors, each naming something to run
(a function call, a constant, a read-only policy check, an SSH session
against the fake device in tests/fake_ssh.py, a whole collect.py run) and
the exact result the reference code produced. Byte strings are
compared as {"bytes": <latin-1 text>}. A result is compared as
canonical JSON (see canonical()), so a difference in type (1 vs 1.0 vs
true, a list vs a string, None vs ""), in a dictionary's key order, or in
any value is a mismatch.

Nothing here contacts a device: sessions run against the fake device.

Usage: python3 conformance/run_kit.py conformance/stage1/kit.json
           [--section NAME ...] [--verbose]

--section runs only the named sections (repeat it for more), to check
one step of a rebuild before the later steps exist. A rebuild is only
accepted on a full run.

Prints one line per section and a final line

    RESULT PASS 512/512 kit 1a2b3c4d5e6f7a8b

that can be read out of a segregated environment: it holds counts and the
first 16 hex digits of the kit file's SHA-256, and no data. On a mismatch
it prints the vector id, the JSON path of the first difference, and the
expected and actual values there. Every input in a kit is synthetic, so
these lines are safe to share too. Exit status 0 on PASS, 1 on FAIL.

The "selftest" vectors carry a deliberately wrong expectation
("expect_mismatch": true). A runner that reports them as matching is not
comparing anything, and the whole run fails.
"""
from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
CONFORMANCE = ROOT / "conformance"
FAKE_SSH = ROOT / "tests" / "fake_ssh.py"
for _p in (str(SCRIPTS), str(CONFORMANCE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

TIME = "<time>"
# Calls that reach outside the process: writing to the device's terminal,
# starting a program, opening a connection. A "calls" vector lists every
# function that makes one, so a new path to a device can't appear unnoticed.
WATCHED_CALLS = ("os.write", "self._send", "os.execvp", "os.execv", "os.execve", "os.fork",
                 "pty.fork", "pty.spawn", "os.system", "os.popen", "os.spawnv", "os.spawnvp")
WATCHED_PREFIXES = ("subprocess.", "socket.", "http.", "urllib.request.", "telnetlib.",
                    "ftplib.", "ssl.")
# Classes of the reference implementation that a stage doesn't have yet.
# Always empty in the rebuild; build_kit.stage_view() sets it.
EXCLUDE_CLASSES = set()
# Every credential variable collect.py reads. Session vectors set all of
# them, so nothing from the runner's own environment leaks in.
CREDENTIAL_VARS = ("NET_USERNAME", "NET_PASSWORD", "NET_ENABLE_PASSWORD",
                   "ANSIBLE_NET_USERNAME", "ANSIBLE_NET_PASSWORD", "ANSIBLE_NET_AUTH_PASS",
                   "NAPALM_USERNAME", "NAPALM_PASSWORD", "PANOS_USERNAME", "PANOS_PASSWORD",
                   "WINDOWS_USERNAME", "WINDOWS_PASSWORD")


# -- canonical form and comparison --------------------------------------------

def jsonable(value):
    """A value as plain JSON types. Tuples become lists, compiled regular
    expressions {"pattern", "flags", "bytes"}; anything else that JSON
    can't hold is an error, so it can't be compared loosely."""
    if isinstance(value, re.Pattern):
        p = value.pattern
        return {"pattern": p.decode("latin-1") if isinstance(p, bytes) else p,
                "flags": value.flags, "bytes": isinstance(p, bytes)}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise TypeError(f"dictionary key {k!r} is not a string")
            out[k] = jsonable(v)
        return out
    if isinstance(value, bytes):
        return {"bytes": value.decode("latin-1")}
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"{type(value).__name__} is not comparable as JSON")


def canonical(value) -> str:
    """The one text form two results are compared in: key order kept,
    ASCII only, no spaces."""
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _kind(v):
    return ("null" if v is None else "bool" if isinstance(v, bool) else
            "int" if isinstance(v, int) else "float" if isinstance(v, float) else
            "str" if isinstance(v, str) else "list" if isinstance(v, list) else "dict")


def first_difference(expected, actual, path="$"):
    """(path, expected, actual) at the first place two JSON values differ,
    or None if they are identical (types and key order included)."""
    if _kind(expected) != _kind(actual):
        return path, expected, actual
    if isinstance(expected, dict):
        if list(expected) != list(actual):
            return path + " (keys)", list(expected), list(actual)
        for k in expected:
            d = first_difference(expected[k], actual[k], f"{path}.{k}")
            if d:
                return d
        return None
    if isinstance(expected, list):
        for i, (e, a) in enumerate(zip(expected, actual)):
            d = first_difference(e, a, f"{path}[{i}]")
            if d:
                return d
        if len(expected) != len(actual):
            return path + " (length)", len(expected), len(actual)
        return None
    return None if canonical(expected) == canonical(actual) else (path, expected, actual)


# -- running one vector ---------------------------------------------------------

def _error(e, first_line=False):
    msg = str(e)
    if first_line:
        msg = msg.split("\n", 1)[0]
    return {"raises": type(e).__name__, "message": msg}


def _module(name):
    return importlib.import_module(name)


@contextlib.contextmanager
def _env(values):
    saved = {k: os.environ.get(k) for k in values}
    os.environ.update({k: v for k, v in values.items()})
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def _fake_ssh_binary():
    """Every CliSession made while this is active runs tests/fake_ssh.py
    instead of the system ssh client."""
    import sshcli
    real = sshcli.CliSession.__init__

    def init(self, *a, **kw):
        kw["ssh_binary"] = str(FAKE_SSH)
        real(self, *a, **kw)
    sshcli.CliSession.__init__ = init
    try:
        yield
    finally:
        sshcli.CliSession.__init__ = real


@contextlib.contextmanager
def _replaced(module, name, value):
    old = getattr(module, name)
    setattr(module, name, value)
    try:
        yield
    finally:
        setattr(module, name, old)


def _scrub(record):
    """A facts record without the parts that depend on when it ran."""
    if not isinstance(record, dict):
        return record
    record = json.loads(json.dumps(record))
    if (record.get("device") or {}).get("collected_at") is not None:
        record["device"]["collected_at"] = TIME
    for entry in (record.get("audit") or {}).get("commands", []):
        if "time" in entry:
            entry["time"] = TIME
    return record


def _log_lines(path):
    return Path(path).read_text().splitlines() if Path(path).exists() else []


def run_attr(v, kit):
    value = getattr(_module(v["module"]), v["name"])
    return {"type": type(value).__name__, "value": jsonable(value)}


def run_call(v, kit):
    func = getattr(_module(v["module"]), v["func"])
    args = [kit if a == "<kit>" else a for a in v.get("args", [])]
    try:
        value = func(*args, **v.get("kwargs", {}))
    except Exception as e:  # noqa: BLE001 - the exception is the result
        return _error(e)
    if v.get("then") and value is not None:
        value = getattr(value, v["then"])
    return {"value": jsonable(value)}


def run_policy(v, kit):
    import readonly
    try:
        policy = readonly.policy_for(v["platform"])
    except Exception as e:  # noqa: BLE001
        return {"policy": _error(e)}
    try:
        check = {"value": policy.check(v["command"])}
    except Exception as e:  # noqa: BLE001
        check = _error(e)
    return {"policy": policy.name, "check": check, "allows": policy.allows(v["command"])}


def run_stdout(v, kit):
    func = getattr(_module(v["module"]), v["func"])
    out = io.StringIO()
    result = {"exit": None}
    with contextlib.redirect_stdout(out):
        try:
            func(*v.get("args", []))
        except SystemExit as e:
            result["exit"] = e.code
    result["stdout"] = out.getvalue().splitlines()
    return result


def run_session(v, kit):
    import readonly
    from sshcli import CliSession
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "device.log"
        env = {"FAKE_SSH_MODE": v["mode"], "FAKE_SSH_LOG": str(log),
               "FAKE_SSH_SLOW_ERASE": ""}
        env.update(v.get("env", {}))
        password = v.get("password", "secret")
        messages = []
        with _env(env):
            policy = readonly.policy_for(v["policy"]) if v.get("policy") else None
            s = CliSession("sw", "admin", password, ssh_binary=str(FAKE_SSH),
                           timeout=10, command_timeout=10, policy=policy)
            result = {}
            try:
                s.open()
                result["open"] = {"prompt": s.prompt.decode()}
            except Exception as e:  # noqa: BLE001
                messages.append(str(e))
                result["open"] = _error(e, first_line=True)
            steps = []
            if "prompt" in result["open"]:
                for step in v.get("steps", []):
                    try:
                        if step[0] == "run":
                            steps.append({"value": s.run(step[1], timeout=step[2])})
                        elif step[0] == "disable_paging":
                            s.disable_paging(step[1])
                            steps.append({"value": None})
                        else:
                            raise ValueError(f"unknown session step {step[0]!r}")
                    except Exception as e:  # noqa: BLE001
                        messages.append(str(e))
                        steps.append(_error(e, first_line=True))
            s.close()
        result["steps"] = steps
        result["audit"] = [{k: e[k] for k in ("command", "kind", "status")} for e in s.audit]
        result["device_log"] = _log_lines(log)
        result["password_in_errors"] = any(password in m for m in messages)
        return result


def _collect_env(v, log):
    env = {k: "" for k in CREDENTIAL_VARS}
    env.update({"FAKE_SSH_MODE": v["mode"], "FAKE_SSH_LOG": str(log), "FAKE_SSH_SLOW_ERASE": ""})
    env.update(v.get("env", {}))
    return env


def run_collect(v, kit):
    import collect
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "device.log"
        out, err = io.StringIO(), io.StringIO()
        result = {}
        with _env(_collect_env(v, log)), _fake_ssh_binary(), \
                _replaced(collect, "commands", lambda platform: list(v["commands"])), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                result["record"] = _scrub(collect.collect(v["name"], v["host"], v["platform"],
                                                          timeout=10, command_timeout=10))
            except Exception as e:  # noqa: BLE001
                result.update(_error(e, first_line=True))
        result["stdout"] = out.getvalue().splitlines()
        result["device_log"] = _log_lines(log)
        result["device_contacted"] = log.exists()
        return result


def run_main(v, kit):
    import collect
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "device.log"
        out_file = Path(tmp) / "facts.json"
        argv = [str(out_file) if a == "<out>" else a for a in v["argv"]]
        out, err = io.StringIO(), io.StringIO()
        result = {"exit": None}
        commands = v.get("commands")
        patch = (_replaced(collect, "commands", lambda platform: list(commands))
                 if commands is not None else contextlib.nullcontext())
        with _env(_collect_env(v, log)), _fake_ssh_binary(), patch, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                collect.main(argv)
            except SystemExit as e:
                # a message exit: its first line (the rest is the device's
                # transcript, which depends on timing)
                result["exit"] = e.code.split("\n", 1)[0] if isinstance(e.code, str) else e.code
        result["stdout"] = out.getvalue().splitlines()
        result["stderr"] = err.getvalue().splitlines()
        result["device_log"] = _log_lines(log)
        result["record"] = (_scrub(json.loads(out_file.read_text(encoding="utf-8")))
                            if out_file.exists() else None)
        return result


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def run_calls(v, kit):
    """[function, call] for every watched call in a file, where function is
    the enclosing "Class.method" or function name ("<module>" at the top
    level), sorted, without repeats."""
    tree = ast.parse((ROOT / v["file"]).read_text(encoding="utf-8"))
    found = set()

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                if not scope and child.name in EXCLUDE_CLASSES:
                    continue
                visit(child, scope + [child.name])
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, scope + [child.name])
            else:
                if isinstance(child, ast.Call):
                    name = _dotted(child.func)
                    if name and (name in WATCHED_CALLS or name.startswith(WATCHED_PREFIXES)):
                        found.add((".".join(scope) or "<module>", name))
                visit(child, scope)
    visit(tree, [])
    return sorted([list(pair) for pair in found])


def run_imports(v, kit):
    """The modules a file imports that are not in the vector's "allowed"
    list (the standard library modules the project uses, and its own
    modules), sorted. Relative imports are the project's own."""
    tree = ast.parse((ROOT / v["file"]).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            names.add(node.module.split(".")[0])
    return sorted(n for n in names if n not in v["allowed"])


RUNNERS = {"attr": run_attr, "call": run_call, "policy": run_policy, "stdout": run_stdout,
           "session": run_session, "collect": run_collect, "main": run_main,
           "calls": run_calls, "imports": run_imports}


def execute(vector, kit):
    """The actual result of one vector, as JSON values. A call argument
    that is the string "<kit>" is replaced by the kit itself."""
    return jsonable(RUNNERS[vector["kind"]](vector, kit))


# -- the whole kit --------------------------------------------------------------

def kit_digest(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def run(kit_path, verbose=False, out=print, sections_wanted=()) -> bool:
    kit = json.loads(Path(kit_path).read_text(encoding="utf-8"))
    digest = kit_digest(kit_path)
    out(f"autotop-awx conformance kit, stage {kit['stage']}, kit {digest}, "
        f"Python {sys.version.split()[0]}")
    sections = {}
    failures = []
    for v in kit["vectors"]:
        if sections_wanted and v["section"] not in sections_wanted:
            continue
        stats = sections.setdefault(v["section"], [0, 0])
        stats[1] += 1
        try:
            actual = execute(v, kit)
        except Exception as e:  # noqa: BLE001 - a runner error is a failure
            actual = {"runner_error": f"{type(e).__name__}: {e}"}
        diff = first_difference(v["expect"], actual)
        if v.get("expect_mismatch"):
            ok = diff is not None
            why = "deliberate mismatch was not detected: the runner is not comparing"
        else:
            ok = diff is None
            why = None
        if ok:
            stats[0] += 1
            if verbose:
                out(f"  ok   {v['id']}")
        else:
            failures.append((v["id"], why, diff))
    if not sections:
        out(f"RESULT FAIL 0/0 kit {digest}: no vectors in sections {', '.join(sections_wanted)}")
        return False
    width = max(len(s) for s in sections)
    for name, (passed, total) in sections.items():
        out(f"  {name:<{width}}  {passed:>4}/{total:<4} {'PASS' if passed == total else 'FAIL'}")
    for vid, why, diff in failures[:25]:
        if why:
            out(f"FAIL {vid}: {why}")
        else:
            path, exp, act = diff
            out(f"FAIL {vid} at {path}\n     expected {canonical(exp)[:300]}"
                f"\n     actual   {canonical(act)[:300]}")
    if len(failures) > 25:
        out(f"... and {len(failures) - 25} more")
    total = sum(t for _, t in sections.values())
    passed = sum(p for p, _ in sections.values())
    scope = f" sections {','.join(sections_wanted)}" if sections_wanted else ""
    out(f"RESULT {'PASS' if not failures else 'FAIL'} {passed}/{total} kit {digest}{scope}")
    return not failures


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    verbose = "--verbose" in argv
    wanted, paths = [], []
    while argv:
        a = argv.pop(0)
        if a == "--section" and argv:
            wanted.append(argv.pop(0))
        elif a != "--verbose":
            paths.append(a)
    if len(paths) != 1:
        sys.exit("usage: run_kit.py <kit.json> [--section NAME ...] [--verbose]")
    sys.exit(0 if run(paths[0], verbose, sections_wanted=tuple(wanted)) else 1)


if __name__ == "__main__":
    main()
