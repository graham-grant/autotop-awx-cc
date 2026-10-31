# autotop-awx production rebuild, stage 1: read-only data gathering

Instructions for Microsoft Copilot · Written 2026-10-31 · Reference: graham-grant/autotop-awx-cc `main` a3e6da7 · Conformance kit: `conformance/stage1/kit.json`, SHA-256 starting `9d80860f96e9ccca`

Supersedes the 2026-10-25 [Copilot rebuild instructions](../2026-10-25/copilot-rebuild-instructions.docx), which rebuilt the whole project in one pass from an older snapshot (before the read-only safeguards existed). This document rebuilds only the data gathering stage, from the current code, in a form that can be checked exactly. The [roadmap](production-rebuild-roadmap.md) lists the later stages and the [validation method](production-rebuild-validation.md) explains every check used here.

## For the person running this (not for Copilot)

1. **Bring two things into the production environment:** this document and the conformance kit file `conformance/stage1/kit.json`. The kit is data (inputs and the exact results the reference code produced), not code. Copilot never writes or edits it.
2. **Check the kit arrived intact** before anything else:
   `sha256sum conformance/stage1/kit.json` must start with `9d80860f96e9ccca`.
3. **Use GitHub Copilot Chat in agent mode** in the production repository, so it can create files and run the checks. Microsoft 365 Copilot also works if you copy each file into the repository yourself.
4. **Paste the Project Rules (Part A) at the start of every Copilot session.** Then paste one step at a time, from `BEGIN STEP` to `END STEP`, and run that step's checks before going on.
5. **When a check fails,** paste the failing lines back to Copilot with: "This check failed. Change the code so it passes. Do not change the kit, the tests or the checks." The kit's failure lines contain only synthetic test data, so you can also send them to me.
6. **Refuse any change to `conformance/stage1/kit.json`, to a test's expected values, or to the read-only policy to make a check pass.** If Copilot proposes one, the code is wrong, not the check.
7. **No device is contacted until step 10.** Steps 1 to 9 run only against the fake device in `tests/fake_ssh.py`.

What to report back after step 10 (all of it is safe to share: counts, rule names and digests, never device data):

- the `RESULT` line of the conformance kit run;
- the `RESULT` line of the unit tests (`Ran N tests ... OK`);
- for each live run: the `audit:` summary line, the `CHECK` line and the `FINGERPRINT` block printed by `check_facts.py`.

---

## Part A: Project Rules (paste at the start of every Copilot session)

```text
PROJECT RULES for autotop-awx, stage 1 (read-only data gathering).
Every file you write must satisfy all of these.

R1  Collection is read-only. Nothing may ever be sent to a network device
    except through CliSession.run() in scripts/sshcli.py, which checks
    every command against the platform's policy in scripts/readonly.py
    first. The only other input ever typed to a device is: the username
    and password at login prompts, "yes" to the ssh host-key question,
    "enable" and the enable secret, a space to answer a pager, Ctrl-C to
    cancel, and "exit". Never answer a confirmation prompt.
R2  Never weaken, broaden or work around the read-only policy, the deny
    list or the character check, even to make a test pass. If a test
    fails, the code under test is wrong.
R3  Standard library only. Python code may import only the Python
    standard library and this project's own modules. No pip packages:
    no Paramiko, Netmiko, NAPALM, pexpect, PyYAML, requests, Jinja2.
    No Ansible collections: playbooks use only ansible.builtin modules.
R4  Python 3.9 compatible. Put "from __future__ import annotations" at
    the top of every module that uses "str | None" or "list[str]"
    annotations. No match statements, no parenthesised context managers,
    nothing added after Python 3.9. The walrus operator (:=) is allowed.
R5  Credentials come only from environment variables. Never put a
    password on a command line, in a file, in a log line or in an
    exception message. Mask passwords as **** in any transcript shown.
R6  Scripts run as "python3 scripts/<name>.py" with scripts/ as the
    import root: modules import each other by bare name ("import
    readonly", "from sshcli import CliSession"); drivers is a package
    ("from drivers import ios").
R7  Match the names given exactly: file names, function and method
    names, class names, constant names, parameter names and order,
    dictionary keys and their order, and every message string. Other
    code checks them by name.
R8  Keep behaviour exactly as specified. Do not add features, options,
    retries, logging frameworks, type checkers or dependencies, and do
    not "improve" a regular expression or a message.
R9  Never modify conformance/stage1/kit.json. It is test data supplied
    from outside. Never edit expected values in tests to make them pass.
R10 Write constants exactly as given, character for character,
    including regular expressions, tuples versus lists, and their order.
```

---

## Part B: Files in stage 1

```text
.gitignore
inventory.yml                    production inventory (you write it; example in step 9)
playbooks/gather_topology.yml    stage 1: check, collect, audit (later stages add plays)
playbooks/conformance.yml        runs the kit and the unit tests, contacts nothing
playbooks/group_vars/all.yml
playbooks/group_vars/network_devices.yml
scripts/config.py
scripts/readonly.py              the read-only policy
scripts/sshcli.py                the only code that talks to a device
scripts/drivers/__init__.py      the schema 2 record and the driver tables
scripts/drivers/common.py
scripts/drivers/ios.py           Cisco IOS / IOS-XE: commands and parsers
scripts/commands.py
scripts/collect.py               one host: plan, collect, write the facts file
scripts/audit.py                 re-checks every command recorded
conformance/run_kit.py           runs the kit
conformance/check_facts.py       checks real facts files, prints a fingerprint
conformance/stage1/kit.json      SUPPLIED, never generated or edited
tests/support.py
tests/fake_ssh.py                a fake device for the tests and the kit
tests/test_readonly.py
tests/test_sshcli.py
tests/test_conformance.py
```

Later stages add the Palo Alto and Windows drivers, link inference, the diagram and its writers, and the feature layers. Don't create any of them now.

---

## Part C: Steps

### Step 1: skeleton and the kit runner

=== BEGIN STEP 1 ===

Create these files. Follow the Project Rules.

**`.gitignore`** with exactly these lines:

```text
output/facts/*
.venv/
__pycache__/
*.pyc
```

**`scripts/config.py`**: a module docstring saying it is the central configuration and has no side effects, and one constant (later stages add more):

```python
# Feature plugins whose data is collected. Later stages draw them; in
# stage 1 they only decide which facet commands are gathered (see
# commands.py). Order matters.
ENABLED_FEATURES = ["ipsec", "vlans", "zones", "roles"]
```

**`tests/support.py`**: puts `scripts/` on `sys.path` so tests can import project modules by bare name.

```python
"""Shared test setup: puts scripts/ on sys.path."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
```

**`conformance/run_kit.py`**: runs a conformance kit against the code. Standard library only. Write it exactly to this specification.

Module-level setup:

- `ROOT` is the repository root (the parent of the `conformance/` directory this file is in). `SCRIPTS = ROOT / "scripts"`, `CONFORMANCE = ROOT / "conformance"`, `FAKE_SSH = ROOT / "tests" / "fake_ssh.py"`. Insert `str(SCRIPTS)` and `str(CONFORMANCE)` at the front of `sys.path` if they are not already in it.
- `TIME = "<time>"`.
- `CREDENTIAL_VARS = ("NET_USERNAME", "NET_PASSWORD", "NET_ENABLE_PASSWORD", "ANSIBLE_NET_USERNAME", "ANSIBLE_NET_PASSWORD", "ANSIBLE_NET_AUTH_PASS", "NAPALM_USERNAME", "NAPALM_PASSWORD", "PANOS_USERNAME", "PANOS_PASSWORD", "WINDOWS_USERNAME", "WINDOWS_PASSWORD")`.
- `WATCHED_CALLS = ("os.write", "self._send", "os.execvp", "os.execv", "os.execve", "os.fork", "pty.fork", "pty.spawn", "os.system", "os.popen", "os.spawnv", "os.spawnvp")`.
- `WATCHED_PREFIXES = ("subprocess.", "socket.", "http.", "urllib.request.", "telnetlib.", "ftplib.", "ssl.")`.
- `EXCLUDE_CLASSES = set()` (always empty here).

Functions:

- `jsonable(value)`: converts a value to plain JSON types, recursively. A compiled regular expression (`re.Pattern`) becomes `{"pattern": <pattern, as text; a bytes pattern decoded as latin-1>, "flags": value.flags, "bytes": <True if the pattern is bytes>}`. A list or tuple becomes a list. A dict becomes a dict with the same key order (raise `TypeError` if a key is not a string). `bytes` becomes `{"bytes": value.decode("latin-1")}`. `None`, `str`, `bool`, `int` and `float` are returned unchanged. Anything else raises `TypeError(f"{type(value).__name__} is not comparable as JSON")`. Check `re.Pattern` first, then list/tuple, dict, bytes, then the plain types.
- `canonical(value)`: `json.dumps(value, ensure_ascii=True, separators=(",", ":"))`.
- `first_difference(expected, actual, path="$")`: returns `None` if the two JSON values are identical, else a tuple `(path, expected_part, actual_part)` for the first difference. Compare the JSON kind first, where the kinds are null, bool, int, float, str, list and dict (bool is not int); different kinds are a difference at `path`. Dicts: if the key lists (in order) differ, return `(path + " (keys)", list(expected), list(actual))`; else recurse into each key with path `f"{path}.{key}"`. Lists: recurse pairwise with path `f"{path}[{i}]"`; then if lengths differ return `(path + " (length)", len(expected), len(actual))`. Anything else: different if their `canonical()` texts differ.
- `kit_digest(path)`: first 16 hex digits of the SHA-256 of the file's bytes.

Running one vector. Each vector is a dict with `"section"`, `"id"`, `"kind"`, `"expect"` and kind-specific fields. `execute(vector, kit)` runs it and returns `jsonable(<result>)`. Exceptions inside the code under test are part of the result, as `{"raises": type(e).__name__, "message": str(e)}`; where "first line" is said below, use only `str(e)` up to the first newline. The kinds:

- `"attr"`: `getattr(importlib.import_module(v["module"]), v["name"])` → `{"type": type(value).__name__, "value": jsonable(value)}`.
- `"call"`: the function `v["func"]` of module `v["module"]`, called with `v.get("args", [])` (any argument equal to the string `"<kit>"` is replaced by the loaded kit dict) and keyword arguments `v.get("kwargs", {})`. An exception gives the raises form. Otherwise, if `v.get("then")` is set and the value is not `None`, the value becomes `getattr(value, v["then"])`. Result `{"value": jsonable(value)}`.
- `"policy"`: `policy = readonly.policy_for(v["platform"])`; if that raises, the result is `{"policy": <raises form>}`. Otherwise `{"policy": policy.name, "check": <{"value": policy.check(command)} or the raises form>, "allows": policy.allows(command)}`.
- `"stdout"`: call the function with `v.get("args", [])` while capturing standard output; catch `SystemExit` and keep its `code`. Result `{"exit": <code or None>, "stdout": <captured text>.splitlines()}`.
- `"session"`: in a fresh temporary directory, with environment variables set for the duration (restore them afterwards) to `FAKE_SSH_MODE=v["mode"]`, `FAKE_SSH_LOG=<tmp>/device.log`, `FAKE_SSH_SLOW_ERASE=""`, then updated with `v.get("env", {})`. `password = v.get("password", "secret")`; `policy = readonly.policy_for(v["policy"]) if v.get("policy") else None`. Create `CliSession("sw", "admin", password, ssh_binary=str(FAKE_SSH), timeout=10, command_timeout=10, policy=policy)`. Call `open()`: on success `result["open"] = {"prompt": s.prompt.decode()}`, on an exception the first-line raises form. Only if it opened, run each step of `v.get("steps", [])`: `["run", command, timeout]` → `{"value": s.run(command, timeout=timeout)}`; `["disable_paging", commands]` → call it, `{"value": None}`; an exception gives the first-line raises form. Keep `str(e)` of every exception (open or step). Then `s.close()`. Result: `{"open": ..., "steps": [...], "audit": [{"command", "kind", "status"} of each entry of s.audit, in that key order], "device_log": <lines of the log file, or [] if it does not exist>, "password_in_errors": <True if password appears in any kept exception text>}`.
- `"collect"`: in a temporary directory; environment = every name in `CREDENTIAL_VARS` set to `""`, then `FAKE_SSH_MODE`, `FAKE_SSH_LOG` (as above) and `FAKE_SSH_SLOW_ERASE=""`, then `v.get("env", {})`. While running, make every `sshcli.CliSession` use the fake device by replacing `CliSession.__init__` with a wrapper that sets `kw["ssh_binary"] = str(FAKE_SSH)` and calls the original; replace `collect.commands` with a function returning `list(v["commands"])`; capture stdout and stderr. Restore everything afterwards. Call `collect.collect(v["name"], v["host"], v["platform"], timeout=10, command_timeout=10)`. Result: `{"record": scrub(record)}` or the first-line raises form, then add `"stdout"` (captured lines), `"device_log"` and `"device_contacted"` (whether the log file exists), in that order.
- `"main"`: like `"collect"`, but replace `collect.commands` only when `v.get("commands")` is not `None`; replace any argument `"<out>"` in `v["argv"]` with `<tmp>/facts.json`; call `collect.main(argv)` and catch `SystemExit`: `"exit"` is its code, or for a string code only its first line (`None` if no SystemExit). Result: `{"exit", "stdout", "stderr", "device_log", "record"}` in that order, where `"record"` is `scrub(<the JSON in facts.json>)` if the file was written, else `None`.
- `"calls"`: parse the file `ROOT / v["file"]` with `ast`. Walk the tree keeping a scope list of enclosing class and function names. Skip a top-level class whose name is in `EXCLUDE_CLASSES`. For every `ast.Call` whose function is a dotted name (a chain of `ast.Attribute` ending in an `ast.Name`, e.g. `os.write`, `self._send`) that is in `WATCHED_CALLS` or starts with one of `WATCHED_PREFIXES`, record `(".".join(scope) or "<module>", dotted_name)`. Result: the sorted unique list of `[scope, name]` pairs.
- `"imports"`: parse the file; collect the first component of every `import x.y` name and of every absolute `from x.y import ...` (skip relative imports); result is the sorted list of those not in `v["allowed"]`.

`scrub(record)`: for a dict, a deep copy (via JSON) in which `device.collected_at`, if not `None`, becomes `TIME`, and every entry of `audit.commands` that has `"time"` gets `TIME`. Other values are returned unchanged.

Running the kit: `run(kit_path, verbose=False, out=print, sections_wanted=())`:

1. Load the kit JSON; print `autotop-awx conformance kit, stage {stage}, kit {digest}, Python {version}` (version is `sys.version.split()[0]`).
2. For each vector (skipping those whose section is not in `sections_wanted`, when it is not empty): count it in its section; compute `actual = execute(v, kit)`, and if executing raises anything, `actual = {"runner_error": f"{type(e).__name__}: {e}"}`. `diff = first_difference(v["expect"], actual)`. If the vector has `"expect_mismatch": true`, it passes only if `diff` is not `None` (it is a deliberately wrong expectation; a runner that doesn't notice is broken), else it fails with the reason `deliberate mismatch was not detected: the runner is not comparing`. Otherwise it passes if `diff` is `None`. With `verbose`, print `  ok   {id}` for each pass.
3. If no section was run, print `RESULT FAIL 0/0 kit {digest}: no vectors in sections {names joined by ", "}` and return False.
4. Print one line per section in kit order: `f"  {name:<{width}}  {passed:>4}/{total:<4} {'PASS' if passed == total else 'FAIL'}"`, where `width` is the longest section name.
5. For the first 25 failures print `FAIL {id}: {reason}` or `FAIL {id} at {path}` followed by `     expected {canonical(expected)[:300]}` and `     actual   {canonical(actual)[:300]}` on two more lines; then `... and N more` if there were more.
6. Print `RESULT {PASS|FAIL} {passed}/{total} kit {digest}` followed by ` sections {names joined by ","}` when sections were selected. Return True only if nothing failed.

`main(argv=None)`: arguments are the kit path, optional `--section NAME` (repeatable) and optional `--verbose`. Anything else: `sys.exit("usage: run_kit.py <kit.json> [--section NAME ...] [--verbose]")`. Exit 0 on PASS, 1 on FAIL. Run `main()` under `if __name__ == "__main__":`.

**`conformance/stage1/kit.json`**: do not create this file. It is supplied. Stop and say so if it is missing.

=== END STEP 1 ===

Checks for step 1:

```bash
sha256sum conformance/stage1/kit.json          # must start with 9d80860f96e9ccca
python3 conformance/run_kit.py conformance/stage1/kit.json --section selftest
# expect: RESULT PASS 4/4 ... sections selftest
```

### Step 2: the read-only policy

=== BEGIN STEP 2 ===

Create **`scripts/readonly.py`**, the guarantee that collection never changes a device. Every command goes through `check()` at the lowest layer, so nothing reaches a device unless the platform's policy allows it. A platform without a policy gets nothing.

Start with `from __future__ import annotations` and `import re`, and a module docstring explaining the three rules below and that a platform without a policy is never contacted.

Exceptions, each with a one-line docstring:

```python
class ReadOnlyViolation(Exception):      # something that could change a device was stopped
class CommandRefused(ReadOnlyViolation):
class UnexpectedPrompt(ReadOnlyViolation):   # the device asked for a confirmation
class ConfigChanged(ReadOnlyViolation):      # the config-change marker moved
```

Constants, exactly (the duplicate "rename" is intentional; keep it):

```python
DENY_VERBS = (
    # Cisco IOS / IOS-XE / NX-OS
    "configure", "write", "copy", "reload", "delete", "erase", "format",
    "clear", "debug", "undebug", "squeeze", "rename", "mkdir", "rmdir",
    "install", "upgrade", "license", "crypto", "no", "default", "archive",
    "event", "tclsh", "test", "send", "boot", "verify", "request",
    "monitor", "start-shell", "guestshell", "app-hosting", "ping",
    "traceroute", "telnet", "ssh", "connect", "release", "renew",
    # PAN-OS
    "set", "commit", "load", "save", "revert", "edit", "move", "rename",
    # Windows / PowerShell (verb half of Verb-Noun)
    "set-", "new-", "remove-", "add-", "enable-", "disable-", "restart-",
    "stop-", "start-", "invoke-", "install-", "uninstall-", "update-",
    "register-", "unregister-", "rename-", "clear-", "reset-", "suspend-",
    "resume-", "move-", "copy-", "out-file",
)

IOS_PIPE_FILTERS = ("include", "exclude", "begin", "section", "count")

_IOS_SHOW = re.compile(r"show( [A-Za-z0-9_./:-]+)+")
_IOS_FILTER = re.compile(r" (%s) [^|?\t]+" % "|".join(IOS_PIPE_FILTERS))
_IOS_SESSION = (
    re.compile(r"terminal length 0"),
    re.compile(r"terminal width (0|[1-9][0-9]{0,2})"),
    re.compile(r"terminal datadump"),     # AT-8000S style switches
)
_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f?]")
```

`class Policy`:

- `__init__(self, name: str, allow, deny_verbs=DENY_VERBS, bad_chars=_BAD_CHARS)`: stores `self.name = name`, `self._allow = allow` (a function command → bool), `self._deny = tuple(deny_verbs)`, `self._bad = bad_chars`.
- `check(self, command: str) -> str`: returns the command unchanged if it is safe, otherwise raises `CommandRefused` with exactly the message shown. The rules run in this order and the first that fails raises:
  1. If `command` is not a `str`, or `command.strip()` is empty: `f"{self.name}: empty command refused"`.
  2. If `self._bad.search(command)`: `f"{self.name}: {command!r} refused: contains a control character, newline or '?'"`.
  3. If `command != command.strip()` or `"  "` (two spaces) is in it: `f"{self.name}: {command!r} refused: stray whitespace"`.
  4. `verb = command.split()[0].lower()`. For each `denied` in `self._deny`, in order: if `denied` ends with `"-"` or equals `"out-file"`, it is a hit when `verb.startswith(denied)`; otherwise it is a hit when `verb == denied` or (`len(verb) >= 2` and `denied.startswith(verb)`), which catches abbreviations such as `conf` and `wr`. On the first hit: `f"{self.name}: {command!r} refused: '{verb}' can change the device ({denied})"`.
  5. If `not self._allow(command)`: `f"{self.name}: {command!r} refused: not in the read-only allowlist for this platform"`.
- `allows(self, command: str) -> bool`: `True` if `check` returns, `False` if it raises `CommandRefused`.

`_ios_allow(command: str) -> bool`: `True` if any pattern in `_IOS_SESSION` full-matches the command. Otherwise split the command on `" |"` (space then pipe) into the first part and the filters; the first part must full-match `_IOS_SHOW`, and every filter must full-match `_IOS_FILTER`.

Policies and lookup:

```python
_POLICIES = {
    "ios": Policy("ios", _ios_allow),
}
```

- `policy_for(platform: str) -> Policy`: `_POLICIES.get((platform or "").lower())`; if `None`, raise `CommandRefused(f"no read-only command policy for platform {platform!r}: refusing to send it anything. Add one to scripts/readonly.py")`.
- `platforms() -> list[str]`: `sorted(_POLICIES)`.

Use `fullmatch` everywhere a rule says "full-match". Do not add other platforms; later stages add them with their own tests.

=== END STEP 2 ===

Checks for step 2:

```bash
python3 conformance/run_kit.py conformance/stage1/kit.json --section policy
# expect: RESULT PASS 418/418 ... sections policy
```

### Step 3: the fake device and the SSH session

=== BEGIN STEP 3 ===

Create **`tests/fake_ssh.py`**, an executable stand-in for the `ssh` binary that tests and the kit run instead of a real device (`#!/usr/bin/env python3` first line; make the file executable: `git update-index --chmod=+x tests/fake_ssh.py`). It ignores its command-line arguments. It reads its behaviour from the environment: `FAKE_SSH_MODE` (default `"inband"`), `FAKE_SSH_LOG` (optional path) and `FAKE_SSH_SLOW_ERASE`.

- `PAGER = "More: <space>,  Quit: q or CTRL+Z, One line: <return> "` (two spaces after the first comma, one trailing space).
- `log(entry)`: if `FAKE_SSH_LOG` is set, append `entry + "\n"` to that file.
- `out(s)`: write `s` to stdout and flush.
- `line(echo=True)`: read stdin one character at a time. On `"\r"`, `"\n"` or `""` (end of input): `out("\r\n")` and return what was read. On `"\x03"` (Ctrl-C): `out("^C\r\n")` and return `"\x03"`. Otherwise echo the character with `out(c)` if `echo`, and keep it.
- Put the terminal in raw mode: `tty.setraw(sys.stdin.fileno())`.
- `out("admin@sw's password: ")`; if `line(echo=False) != "secret"`: `out("Permission denied, please try again.\r\n")` and `sys.exit(255)`.
- Unless the mode is `"plain"`, log in again in-band: `out("\r\n\r\nUser Name:")`, `user = line()`, `out("Password:")`, `pw = line(echo=False)`. If `user != "admin"` or `pw != "secret"` or the mode is `"wrongpass"`: `out("Login invalid\r\n\r\nUser Name:")`, `line()`, `sys.exit(1)`.
- `paging = True`, `config_reads = 0`. Then loop forever: `out("AT-8K-1# ")`, `cmd = line()`, `log("ctrl-c" if cmd == "\x03" else cmd)`, and then:
  - `"\x03"`: continue.
  - `"terminal datadump"`: `paging = False`.
  - `"show interfaces status"`: for `i` from 1 to 12: `out(f"g{i}  1G-Copper  Full  1000  Up\r\n")`; when `paging` and `i % 5 == 0`: `out(PAGER)`, read one character, `out("\r" + " " * len(PAGER) + "\r")`, and if `FAKE_SSH_SLOW_ERASE` is set (non-empty), `time.sleep(0.3)`.
  - `"show running-config | include configuration change"`: `config_reads += 1`; `minute = 10 + (config_reads if MODE == "configchange" else 0)`; `out(f"! Last configuration change at 09:{minute}:00 UTC Mon Oct 26 2026 by admin\r\n")`.
  - `"show confirm-test"`: `out("Proceed with reload? [confirm]")`; read one character; `log("CONFIRMED" if it != "\x03" else "ctrl-c")`; `out("^C\r\n")`.
  - `"show hang"`: read characters until one is `"\x03"`; `log("ctrl-c")`; `out("^C\r\n")`.
  - `"show version"`: `out("SW version    3.0.0.44\r\n")` (four spaces).
  - `"exit"` or `"logout"`: `sys.exit(0)`.
  - any other non-empty command: `out("% Unrecognized command\r\n")`.

Create **`scripts/sshcli.py`**: interactive CLI sessions over SSH, standard library only. It drives the system `ssh` client through a pseudo-terminal (`pty`) and answers prompts as a person would. Read-only by construction: `run()` sends a command only if the session's policy allows it, never answers a confirmation prompt (it cancels with Ctrl-C and raises), and records every line it sends in `audit`. Credentials are written to the pty, never put on a command line. Include a module docstring that says this.

Imports: `from __future__ import annotations`, `errno`, `os`, `pty`, `re`, `select`, `shutil`, `signal`, `time`, `from datetime import datetime, timezone`, `import readonly`. Nothing else.

Constants, exactly:

```python
_ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Za-z0-9]|\x1b[=>]")
# Paging prompts, removed from captured output after we answer them
_MORE = re.compile(rb"(?:--\s*More\s*--|<--- More --->|"
                   rb"More: <space>,\s*Quit: q[^,]*,\s*One line: <return>\s?)"
                   rb"(?:\x08+\s*\x08+|\r\s+\r)?", re.IGNORECASE)
# A pager prompt still waiting for a key (nothing printed after it yet)
_MORE_WAITING = re.compile(rb"(?:--\s*More\s*--|<--- More --->|One line: <return>)\s*$",
                           re.IGNORECASE)
_USER_PROMPT = re.compile(rb"(?:user\s*name|username|login)\s*:\s*$", re.IGNORECASE)
_PASS_PROMPT = re.compile(rb"password\s*:\s*$", re.IGNORECASE)
_HOSTKEY_PROMPT = re.compile(rb"continue connecting.*\?\s*$", re.IGNORECASE)
# A CLI prompt at the end of the buffer: "AT-8K-1#", "core1>", "sw(config)#"
_CLI_PROMPT = re.compile(rb"(?:^|[\r\n])([^\r\n]{1,80}?[>#])\s*$")
# A device asking us to confirm something. A read-only command never does
# this, so it is cancelled, never answered.
_CONFIRM_PROMPT = re.compile(rb"(?:\[confirm\]|\[yes/no\]|\(yes/no\)\??|\[y/n\]|\(y/n\)\??|"
                             rb"\]\?)\s*:?\s*$", re.IGNORECASE)
_CTRL_C = b"\x03"
_AUTH_FAILED = re.compile(rb"(permission denied|authentication failed|access denied|"
                          rb"login invalid|bad (user ?name|password)|% ?login invalid)",
                          re.IGNORECASE)
```

`class CliError(RuntimeError)` and `class AuthError(CliError)`, both with `pass` bodies.

`class CliSession`:

- `__init__(self, host: str, username: str, password: str, port: int = 22, enable_password: str | None = None, ssh_args: list[str] | None = None, timeout: float = 30.0, command_timeout: float = 120.0, ssh_binary: str = "ssh", policy: readonly.Policy | None = None, log=None)`. Sets `self.host`, `self.port = int(port)`, `self.username`, `self.password`, `self.enable_password`, `self.ssh_args = list(ssh_args or [])`, `self.timeout`, `self.command_timeout`, `self.ssh_binary`, `self.pid = None`, `self.fd = None`, `self.prompt: bytes | None = None`, `self.transcript = bytearray()`, `self.policy = policy`, `self.log = log or (lambda line: None)`, `self.audit: list[dict] = []` and `self._abandoned = None`.
- `_spawn(self)`: if `shutil.which(self.ssh_binary)` is falsy, raise `CliError(f"'{self.ssh_binary}' not found - install the OpenSSH client")`. Build `argv = [self.ssh_binary, "-tt", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=keyboard-interactive,password", "-o", "NumberOfPasswordPrompts=1", "-o", f"ConnectTimeout={int(self.timeout)}", "-o", "ServerAliveInterval=15", "-p", str(self.port), *self.ssh_args, f"{self.username}@{self.host}"]`. `pid, fd = pty.fork()`; in the child (`pid == 0`): `os.execvp(argv[0], argv)` inside `try`, with `os._exit(127)` in `finally`. In the parent store `self.pid, self.fd`.
- `_send(self, text: str)`: `os.write(self.fd, text.encode() + b"\r")`.
- `_read_some(self, deadline: float) -> bytes`: `remaining = deadline - time.monotonic()`; if `<= 0` return `b""`. `select.select([self.fd], [], [], min(remaining, 1.0))`; if not ready return `b""`. `data = os.read(self.fd, 65536)`; an `OSError` with `errno.EIO` (the child exited) raises `CliError(f"{self.host}: connection closed\n{self._tail()}")`, any other `OSError` is re-raised; empty `data` raises the same `CliError`. Append `data` to `self.transcript` and return it.
- `_record(self, command: str, kind: str, status: str) -> dict`: `entry = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "command": command, "kind": kind, "status": status}` (keys in that order); append to `self.audit`; call `self.log(f"{status:>8}  {command}")`; return `entry`.
- `_tail(self, n=600) -> str`: the last `n` bytes of the transcript with `_ANSI` removed, decoded as UTF-8 with `"replace"`; then each of `self.password` and `self.enable_password` that is set is replaced by `"****"`.
- `open(self)`: `self._spawn()`; `buf = b""`; `deadline = time.monotonic() + self.timeout`; `password_prompts = user_prompts = 0`. Loop:
  - if past the deadline: `CliError(f"{self.host}: timed out waiting to log in\n{self._tail()}")`;
  - `data = self._read_some(deadline)`; if empty, continue; `buf += data`; `clean = _ANSI.sub(b"", buf).rstrip(b" ")`;
  - if `_AUTH_FAILED.search(clean)`: `AuthError(f"{self.host}: login rejected\n{self._tail()}")`;
  - elif `_HOSTKEY_PROMPT.search(clean)`: `self._send("yes")`;
  - elif `_PASS_PROMPT.search(clean)`: `password_prompts += 1`; if `password_prompts > 2 + user_prompts`: `AuthError(f"{self.host}: password rejected\n{self._tail()}")`; else `self._send(self.password)`;
  - elif `_USER_PROMPT.search(clean)`: `user_prompts += 1`; if `user_prompts > 2`: `AuthError(f"{self.host}: username rejected\n{self._tail()}")`; else `self._send(self.username)`;
  - elif `_MORE_WAITING.search(clean)`: `os.write(self.fd, b" ")` (a paged login banner);
  - else: `m = _CLI_PROMPT.search(clean)`; if no match, continue (without resetting); otherwise `self.prompt = m.group(1).strip()` and leave the loop;
  - after any of the answered branches above: `buf = b""` and `deadline = time.monotonic() + self.timeout`.
  After the loop, if `self.prompt.endswith(b">")` and `self.enable_password`: `self._enable()`. Return `self`.
- `_enable(self)`: `self._record("enable", "session", "sent")`; `self._send("enable")`; `deadline = time.monotonic() + self.timeout`; `buf = b""`. While before the deadline: `buf += self._read_some(deadline)`; `clean = _ANSI.sub(b"", buf).rstrip(b" ")`; if `_PASS_PROMPT.search(clean)`: send the enable password and reset `buf`; elif `_CLI_PROMPT.search(clean)` matches and its group 1, stripped, ends with `b"#"`: set `self.prompt` to it and return. After the loop: `CliError(f"{self.host}: 'enable' failed\n{self._tail()}")`.
- `_prompt_re(self) -> re.Pattern`: `base = re.escape(re.sub(rb"[>#]$", b"", self.prompt).split(b"(")[0])`; return `re.compile(rb"(?:^|[\r\n])" + base + rb"(?:\([^)]*\))?[>#]\s*$")`.
- `run(self, command: str, timeout: float | None = None, kind: str = "collect") -> str`: runs one command and returns its output without the echoed command or the trailing prompt.
  1. If `self.prompt is None`: `CliError("session not open")`.
  2. If `self._abandoned`: `CliError(f"{self.host}: session abandoned ({self._abandoned}), not sending {command!r}")`.
  3. If `self.policy is None`: `self._record(command, kind, "refused")` then raise `readonly.CommandRefused(f"{self.host}: no read-only policy for this session, not sending {command!r}")`.
  4. `self.policy.check(command)`; if it raises `readonly.CommandRefused`, `self._record(command, kind, "refused")` and re-raise.
  5. `prompt_re = self._prompt_re()`; `entry = self._record(command, kind, "sent")`; `self._send(command)`; `deadline = time.monotonic() + (timeout or self.command_timeout)`; `buf = b""`; `answered = 0`.
  6. Loop: if past the deadline: `entry["status"] = "timeout"`, `self._cancel(prompt_re, f"timed out running {command!r}")`, raise `CliError(f"{self.host}: timed out running {command!r}\n{self._tail()}")`. `data = self._read_some(deadline)`; if empty continue; `buf += data`; `tail = _ANSI.sub(b"", buf[-200:])`. If `_MORE_WAITING.search(_ANSI.sub(b"", buf[max(answered, len(buf) - 200):]))`: `os.write(self.fd, b" ")`, `answered = len(buf)`, continue (only what arrived since the last answered pager is examined, so a pager's erase arriving on its own isn't answered twice). If `prompt_re.search(tail.rstrip(b" "))`: leave the loop. If `_CONFIRM_PROMPT.search(tail.rstrip(b" "))`: `entry["status"] = "cancelled"`, `self._cancel(prompt_re, f"{command!r} asked for confirmation")`, `self._abandoned = f"{command!r} asked for confirmation"`, raise `readonly.UnexpectedPrompt(f"{self.host}: {command!r} asked for confirmation, which a read-only command never does. Cancelled with Ctrl-C, not answered.\n{self._tail()}")`.
  7. `entry["status"] = "ok"`; return `self._clean(buf, command, prompt_re)`.
- `_cancel(self, prompt_re, why: str)`: `self.log(f"{'ctrl-c':>8}  ({why})")`. `os.write(self.fd, _CTRL_C)`; on `OSError` set `self._abandoned = why` and return. `deadline = time.monotonic() + min(self.timeout, 10.0)`; `buf = b""`; while before the deadline: `buf += self._read_some(deadline)` (a `CliError` ends the loop); if `prompt_re.search(_ANSI.sub(b"", buf[-200:]).rstrip(b" "))`: return. After the loop: `self._abandoned = why`.
- `_clean(buf: bytes, command: str, prompt_re) -> str` (a `@staticmethod`): `text = _ANSI.sub(b"", buf)`; `text = _MORE.sub(b"", text)`; apply backspaces by repeating `text = re.sub(rb"[^\x08\n]\x08", b"", text)` until it stops changing, then remove any remaining `b"\x08"`; `text = re.sub(rb"\r+\n", b"\n", text)`; `lines = [l.rsplit("\r", 1)[-1] for l in text.decode("utf-8", "replace").split("\n")]`; if `lines` and `command in lines[0]`, drop the first line; while `lines` and (the last line is blank or `prompt_re.search(lines[-1].encode())`), drop the last line; return `"\n".join(lines) + "\n"`.
- `disable_paging(self, commands: list[str])`: for each command, `self.run(cmd, timeout=self.timeout, kind="session")`, ignoring `CliError` only (a `readonly.CommandRefused` still raises).
- `close(self)`: if `self.fd is None` return. In a `try` (ignore `OSError`): if `self.prompt is not None`, `self._record("exit", "session", "sent")`; `self._send("exit")`; `time.sleep(0.2)`. Then `os.close(self.fd)` (ignore `OSError`), `os.kill(self.pid, signal.SIGTERM)` (ignore `ProcessLookupError`), `os.waitpid(self.pid, 0)` (ignore `ChildProcessError`), and `self.fd = None`.
- `__enter__` returns `self.open()`; `__exit__(self, *exc)` calls `self.close()`.

Nothing else in this module may write to the device or start a process. Do not add a PowerShell or API session class (later stages).

=== END STEP 3 ===

Checks for step 3:

```bash
python3 conformance/run_kit.py conformance/stage1/kit.json --section session
# expect: RESULT PASS 13/13 ... sections session
```

### Step 4: the Cisco IOS driver and the device record

=== BEGIN STEP 4 ===

Vendor knowledge lives only in `scripts/drivers/`. Each driver turns one platform's output into the same vendor-neutral "schema 2" record.

Create **`scripts/drivers/common.py`** (`from __future__ import annotations`, `import re`):

```python
_HEX = re.compile(r"[0-9a-fA-F]")
_NAME = re.compile(r"^([A-Za-z][A-Za-z-]*?)\s*(\d\S*)$")
```

- `norm_mac(text: str | None) -> str | None`: any common MAC spelling to `"52:54:00:12:34:56"`. If `text` is falsy, `None`. `digits = "".join(_HEX.findall(text))`; if `len(digits) != 12` or `re.search(r"[^0-9a-fA-F.:\-\s]", text)`, `None`. Otherwise lower-case the digits and join them in pairs with `":"`.
- `canonical_intf(name: str | None, known) -> str | None`: resolves an abbreviated interface name against the device's own names. If `name` is falsy return it. `known = list(known)`. Exact match: return `name`. Case-insensitive exact match: return that known name. `m = _NAME.match(name.strip())`; no match: return `name`. `prefix, rest = m.group(1).lower(), m.group(2)`; the hits are the known names `k` where `_NAME.match(k)` matches, its group 2 equals `rest`, and its group 1, lower-cased, starts with `prefix`. Return the hit if there is exactly one, else `name`.

Create **`scripts/drivers/ios.py`** (`from __future__ import annotations`, `import re`, `from drivers.common import canonical_intf, norm_mac`). Constants, exactly:

```python
TRANSPORT = "ssh-cli"
VENDOR = "cisco"

CORE_COMMANDS = ["show version", "show interfaces", "show ip interface",
                 "show lldp neighbors detail", "show cdp neighbors detail",
                 "show ip arp", "show mac address-table dynamic"]
PAGING_COMMANDS = ["terminal length 0"]
# Run before and after collection: the "! Last configuration change at ...
# by ..." line. If it moves while we are logged in, collection fails loudly.
CONFIG_CHECK_COMMAND = "show running-config | include configuration change"
# facet -> commands it is parsed from
FACET_COMMANDS = {"ipsec": ["show crypto session detail"],
                  "vlans": ["show vlan brief", "show interfaces trunk"]}

_INTF_HEADER = re.compile(
    r"^(?P<name>\S+) is (?P<admin>administratively down|up|down|deleted)\S*,"
    r"\s+line protocol is (?P<proto>\w+)"
)
_MAC = re.compile(r"address is ([0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4})")
_MTU = re.compile(r"MTU (\d+) bytes")
_BW = re.compile(r"BW (\d+) Kbit")
_DOT1Q = re.compile(r"Encapsulation 802\.1Q Virtual LAN, Vlan ID\s+(\d+)")
_IP_PRIMARY = re.compile(r"Internet address is (\d+\.\d+\.\d+\.\d+)/(\d+)")
_IP_SECONDARY = re.compile(r"Secondary address (\d+\.\d+\.\d+\.\d+)/(\d+)")
_VRF = re.compile(r'VPN Routing/Forwarding "([^"]+)"')
_HOSTNAME = re.compile(r"^(\S+) uptime is", re.MULTILINE)
_VERSION = re.compile(r"^Cisco IOS.*?Version ([^\s,]+)", re.MULTILINE)
_MODEL = re.compile(r"^cisco (\S+)(?: \([^)]*\))? .*?processor", re.MULTILINE | re.IGNORECASE)
_SERIAL = re.compile(r"^Processor board ID (\S+)", re.MULTILINE)
_MEMBERS = re.compile(r"Members in this channel:\s*(.+)")
_DOTTED_MAC = r"[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}"
_ARP_LINE = re.compile(r"^Internet\s+(\d+\.\d+\.\d+\.\d+)\s+\S+\s+(" + _DOTTED_MAC +
                       r")\s+\S+\s*(\S*)\s*$")
_MAC_LINE = re.compile(r"^\s*(\d+|All)\s+(" + _DOTTED_MAC + r")\s+\S+\s+(?:\S+\s+)*?"
                       r"([A-Za-z][\w./:-]*\d)\s*$")
_VLAN_LINE = re.compile(r"^(\d+)\s+(\S+)\s+(active|act/\S+|suspended|sus\S*)\s*(.*)$")
_CDP_CAPS = {"router": "router", "switch": "bridge", "trans-bridge": "bridge",
             "source-route-bridge": "bridge", "host": "station", "phone": "phone"}
_LLDP_CAPS = {"R": "router", "B": "bridge", "T": "phone", "W": "wlan", "S": "station",
              "C": "docsis", "O": "other", "P": "repeater"}
_SA_RE = re.compile(r"IKEv[12] SA:\s+local\s+([0-9a-fA-F:.]+)/\d+\s+remote\s+([0-9a-fA-F:.]+)/\d+")
_FLOW_RE = re.compile(r"IPSEC FLOW:.*?host\s+([0-9a-fA-F:.]+)\s+host\s+([0-9a-fA-F:.]+)")
# Models that are switches rather than routers
_SWITCH_MODEL = re.compile(r"^(WS-C|C9[2-6]\d\d|C1000|C2960|C3[68]50|IE-)", re.IGNORECASE)

_KINDS = (("Loopback", "loopback"), ("Tunnel", "tunnel"), ("Port-channel", "lag"),
          ("Vlan", "vlan"), ("BDI", "vlan"), ("NVI", "virtual"), ("Null", "virtual"),
          ("Virtual", "virtual"))
```

Helper functions:

- `_mac(dotted: str) -> str`: remove the dots, lower-case, join in pairs with `":"`.
- `_blocks(text: str)`: a generator over `(header match, block lines)` per interface. For each line of `(text or "").splitlines()`: if `_INTF_HEADER.match(line)` matches, yield the previous header and its lines (if there was one) and start a new block; otherwise, if inside a block, append `line.strip()`. Yield the last block at the end.
- `_kind_and_parent(name: str) -> tuple[str, str | None]`: if `"."` is in the name, `("subinterface", <the part before the first ".">)`; else the kind of the first `_KINDS` prefix the name starts with, `(kind, None)`; else `("physical", None)`.
- `_caps(words, table) -> list[str]`: for each word, look it up in `table` (as is when `table is _LLDP_CAPS`, otherwise lower-cased); add each found value once, in order.
- `_neighbor(protocol, local, remote_name, remote_intf=None, remote_mac=None, remote_ip=None, platform=None, caps=()) -> dict`: returns `{"local_intf": local, "protocol": protocol, "remote_name": remote_name, "remote_intf": remote_intf, "remote_mac": remote_mac, "remote_ip": remote_ip, "remote_platform": platform, "remote_capabilities": list(caps)}` in that key order.
- `_entries(text: str, sep: str)`: a generator of blocks of lines separated by lines that, stripped, start with `sep` and consist only of `"-"` characters. Lines are kept with `rstrip()`. Yield each non-empty block, including the last.

Parsers (every one accepts `""` and returns an empty result):

- `parse_show_version(text) -> dict`: `text = text or ""`. For the keys `hostname`, `os_version`, `model`, `serial` in that order, search `_HOSTNAME`, `_VERSION`, `_MODEL`, `_SERIAL`; the value is group 1 or `None`. Then `found["role"] = "switch" if _SWITCH_MODEL.match(model or "") else "router"`. Key order: hostname, os_version, model, serial, role.
- `parse_show_interfaces(text) -> dict`: `{name: interface}` in output order. For each block from `_blocks`: `name = header["name"]`, `body = "\n".join(lines)`; `description` is the text after the first `":"` of the last line starting with `"Description:"`, stripped (default `""`); search `_MAC`, `_MTU`, `_BW`, `_DOT1Q` in `body`; `kind, parent = _kind_and_parent(name)`; `vlan = int(dot1q group 1)` if found, else, when `kind == "vlan"` and `name[4:].isdigit()`, `int(name[4:])`, else `None`; `speed = int(bw group 1) / 1000` if found else `None`. The interface is, in this key order: `"name"`, `"kind"`, `"mac": _mac(...)` or `None`, `"admin_up": header["admin"] != "administratively down"`, `"oper_up": header["proto"] == "up"`, `"speed_mbps": int(speed) if speed is not None and speed == int(speed) else speed`, `"mtu": int(...)` or `None`, `"description"`, `"vrf": "default"`, `"vlan"`, `"parent"`, `"ipv4": []`, `"ipv6": []`. If `kind == "lag"` and `_MEMBERS.search(body)` matches, store its group 1 split on whitespace under a temporary key `"_members"`. After all blocks: for each interface, pop `"_members"` (default `[]`) and for each member set `parent` of `interfaces.get(canonical_intf(member, interfaces))` (if it exists) to the port-channel's name.
- `parse_show_ip_interface(text) -> dict`: `{name: {"vrf": vrf, "ipv4": [...]}}`. For each block: `ipv4 = []`, `vrf = "default"`; for each line, if `_IP_PRIMARY.search(l)` or else `_IP_SECONDARY.search(l)` matches, append `f"{group1}/{group2}"`; otherwise, if `_VRF.search(l)` matches, `vrf` is its group 1.
- `parse_cdp_neighbors(text) -> list[dict]`: for each block of `_entries(text, "---")`, read stripped lines into a dict `f`: `"Device ID:"` → `f["name"]` = the text after the first `":"`, stripped, with a trailing `"(...)"` removed (`re.sub(r"\(.*\)$", "", ...)`); `"IP address:"` → `f["ip"]` only if not already set; `"Platform:"` → split the line on the first `","` into `plat` and `caps`: `f["platform"]` = `plat` after its first `":"`, stripped, and `f["caps"]` = `caps` after its first `":"` split on whitespace, or `[]` if `caps` has no `":"`; `"Interface:"` → split on the first `","` into `local` and `port`: `f["local"]` = `local` after its first `":"`, stripped, and `f["port"]` = `port` after its first `":"`, stripped, or `None` if no `":"`. If `f` has a `"name"` and a `"local"`, append `_neighbor("cdp", f["local"], f["name"], f.get("port"), None, f.get("ip"), f.get("platform"), _caps(f.get("caps", []), _CDP_CAPS))`.
- `parse_lldp_neighbors(text) -> list[dict]`: for each block of `_entries(text, "---")`, for each stripped line, `key, _, value = s.partition(":")` and `value = value.strip()`; map `"Local Intf"` → `local`, `"Chassis id"` → `chassis`, `"Port id"` → `port`, `"Port Description"` → `port_desc`, `"System Name"` → `name`, `"Enabled Capabilities"` → `caps` (split on `","`, each stripped, empty ones dropped), and `"IP"` → `ip` only if not already set. Skip the block if there is no `local`. `port_mac = norm_mac(f.get("port"))`; `remote_intf = f.get("port_desc") if port_mac else f.get("port")`; `name = f.get("name") or None`. Append `_neighbor("lldp", f["local"], name, remote_intf or None, port_mac or norm_mac(f.get("chassis")), f.get("ip"), None, _caps(f.get("caps", []), _LLDP_CAPS))`.
- `parse_arp(text) -> list[dict]`: for each line, `_ARP_LINE.match(line.strip())`; append `{"ip": g1, "mac": norm_mac(g2), "intf": g3 or None, "vrf": "default"}`.
- `parse_mac_table(text) -> list[dict]`: for each line (not stripped), `_MAC_LINE.match(line)`; skip non-matches and lines whose group 3, upper-cased, is one of `"CPU"`, `"DROP"`, `"ROUTER"`, `"SWITCH"`; append `{"mac": norm_mac(g2), "intf": g3, "vlan": int(g1) if g1.isdigit() else None}`.
- `parse_vlan_brief(text) -> list[dict]`: `vlans`, `current = None`. For each line: if `_VLAN_LINE.match(line)`: `current = {"id": int(g1), "name": g2, "ports": <g4 split with re.split(r"[,\s]+", ...), empty parts dropped>}`, appended unless `1002 <= id <= 1005`; continue. Otherwise, if `current` is not `None` and the line starts with `" "` and isn't blank, extend `current["ports"]` with the stripped line split the same way.
- `parse_interfaces_trunk(text) -> list[dict]`: `trunks = {}`, `section = None`. For each non-blank stripped line `s`: if it starts with `"Port"`, `section` becomes `"mode"` if `"Native"` is in it, else `"active"` if `"active in"` is in it, else `"allowed"` if `"allowed on trunk"` is in it, else `None`; continue. Otherwise `port, _, rest = s.partition(" ")`, `rest = rest.strip()`. In `"mode"`: `native = rest.split()[-1] if rest else None`; `trunks[port] = {"port": port, "native": int(native) if native and native.isdigit() else None, "allowed": "", "active": ""}`. In `"allowed"` or `"active"`, if `port in trunks`: set that key to `""` if `rest.lower() == "none"`, else `rest` with spaces removed. Return `list(trunks.values())`.
- `parse_crypto_session(text) -> list[dict]`: tunnels from blocks starting `"Interface:"`. `current = None`. `flush()` appends `current` if it is set and has a `peer_address`. For each stripped line `s`: `"Interface:"` → flush, then `current = {"interface": <after ":", stripped>, "local_address": None, "peer_address": None, "status": "unknown", "vendor_status": None}`; else if `current is None` continue; `"Session status:"` → `word` = after `":"`, stripped; `vendor_status = word`; `status = "up"` if `word.upper().startswith("UP")`, `"down"` if it starts with `"DOWN"`, else `"unknown"`; `"Peer:"` and `peer_address is None` → `s.split()[1]`; else if `_SA_RE.search(s)` matches and `local_address is None` → `local_address = g1`, `peer_address = peer_address or g2`; else if `_FLOW_RE.search(s)` matches and `local_address is None` → if `g2 == peer_address`, `local_address = g1`. Flush at the end.
- `parse_core(raw: dict) -> dict`: `version = parse_show_version(raw.get("show version", ""))`; `interfaces = parse_show_interfaces(raw.get("show interfaces", ""))`. For each `name, ip` of `parse_show_ip_interface(raw.get("show ip interface", ""))`: if the interface isn't known, create it with `kind, parent = _kind_and_parent(name)` as `{"name", "kind", "mac": None, "admin_up": None, "oper_up": None, "speed_mbps": None, "mtu": None, "description": "", "vrf": "default", "vlan": None, "parent", "ipv4": [], "ipv6": []}` (that order) and add it; then set its `"vrf"` and `"ipv4"` from `ip`. `names = list(interfaces)`. `neighbors = parse_lldp_neighbors(raw.get("show lldp neighbors detail", "")) + parse_cdp_neighbors(raw.get("show cdp neighbors detail", ""))`, each `local_intf` replaced by `canonical_intf(..., names)`. `arp = parse_arp(raw.get("show ip arp", ""))`, each `intf` canonicalised, then `vrf` = that interface's `vrf` (`"default"` if unknown). `mac_table = parse_mac_table(raw.get("show mac address-table dynamic", ""))`, each `intf` canonicalised. Return `{"device": {"hostname", "role", "vendor": VENDOR, "model", "os_version", "serial"}, "interfaces": list(interfaces.values()), "neighbors", "arp", "mac_table"}` (device keys in that order, from `version`).
- `parse_facets(raw: dict) -> dict`: `facets = {}`. If `raw.get("show crypto session detail")` is not `None`: `facets["ipsec"] = {"tunnels": parse_crypto_session(text)}`. If `raw.get("show vlan brief")` is not `None`: `names = list(parse_show_interfaces(raw.get("show interfaces", "")))`; `vlans = parse_vlan_brief(...)` with each port canonicalised against `names`; `trunks = parse_interfaces_trunk(raw.get("show interfaces trunk", ""))` with each `port` canonicalised; `facets["vlans"] = {"vlans": vlans, "trunks": trunks}`.

Create **`scripts/drivers/__init__.py`** (`from __future__ import annotations`, `from drivers import ios`), with a docstring describing the schema 2 record:

```python
SCHEMA = 2

_DRIVERS = {"ios": ios}

# Ansible network_os / NAPALM driver names -> driver
_ALIASES = {
    "ios": "ios", "iosxe": "ios", "ios-xe": "ios", "cisco.ios.ios": "ios",
    "panos": "panos", "pan-os": "panos", "paloaltonetworks.panos": "panos",
    "windows": "windows", "win": "windows",
}

# Tried in order after login when a driver doesn't say
_DEFAULT_PAGING = ["terminal length 0", "terminal datadump"]

CORE_KEYS = ("interfaces", "neighbors", "arp", "mac_table")
```

(The PAN-OS and Windows aliases are kept on purpose: those names map to platforms that have no driver and no read-only policy yet, so they are refused.)

- `platform_key(network_os)`: `_ALIASES.get((network_os or "").lower(), (network_os or "").lower())`.
- `driver_for(network_os)`: `_DRIVERS.get(platform_key(network_os))`.
- `platforms() -> list[str]`: `sorted(_DRIVERS)`.
- `paging_commands(network_os)`: `list(getattr(driver_for(network_os), "PAGING_COMMANDS", _DEFAULT_PAGING))`.
- `core_commands(network_os)`: `list(driver.CORE_COMMANDS)` if there is a driver, else `[]`.
- `config_check_command(network_os)`: `getattr(driver_for(network_os), "CONFIG_CHECK_COMMAND", None)`.
- `config_marker(network_os, output)`: the driver's `config_marker(output)` if it has one, else `output.strip()`.
- `transport(network_os)`: `getattr(driver_for(network_os), "TRANSPORT", None)`.
- `facet_commands(network_os, facets=None) -> list[str]`: `table = getattr(driver_for(network_os), "FACET_COMMANDS", {})`; for each facet of `table` (when `facets` is `None`) or of `facets`, add each of `table.get(facet, [])` not already added, in order.
- `_empty(name, platform) -> dict`: `{"schema": SCHEMA, "device": {"name": name, "hostname": name, "role": None, "vendor": None, "platform": platform_key(platform) or None, "model": None, "os_version": None, "serial": None, "collected_at": None, "collector": None}, "interfaces": [], "neighbors": [], "arp": [], "mac_table": [], "facets": {}, "raw": {}}` (that key order).
- `build_record(name, platform, raw, collected_at=None, collector=None) -> dict`: `record = _empty(name, platform)`; `record["raw"] = dict(raw)`; `record["device"]["collected_at"] = collected_at`. If there is no driver: if `raw` is not empty, `print(f"drivers: no driver for platform {platform!r} ({name}) - no interface data")`; return the record. Set `device["collector"] = collector or driver.TRANSPORT` and `device["vendor"] = driver.VENDOR`. In a `try`: `core = driver.parse_core(raw)`, `facets = driver.parse_facets(raw)`; on any `Exception` print `f"drivers: {name}: {platform_key(platform)} parser failed ({e!r}) - raw output kept, no interface data"`, set `record["parse_error"] = repr(e)` and return the record. Copy each non-`None` value of `core.pop("device", {})` into `record["device"]`; then `device["hostname"] = device["hostname"] or name`; set each of `CORE_KEYS` from `core.get(key, [])`; `record["facets"] = facets`; return it.
- `name(record)`: `record["device"]["name"]`.
- `interface(record, intf_name)`: the first interface dict with that name, or `None`.

Do not write `load()`, `reparse()` or any PAN-OS or Windows code (later stages).

=== END STEP 4 ===

Checks for step 4:

```bash
python3 conformance/run_kit.py conformance/stage1/kit.json --section parsers --section records
# expect: RESULT PASS ... sections parsers,records
```

### Step 5: the command list and the collector

=== BEGIN STEP 5 ===

Create **`scripts/commands.py`**, which prints (as a JSON list) every command collection runs for a platform: the driver's core commands, then the commands behind each enabled feature's facet.

```python
#!/usr/bin/env python3
import json
import sys

import config
import drivers

# Which facet each feature draws. Later stages replace this table with the
# feature plugins themselves.
FEATURE_FACETS = {"ipsec": "ipsec", "vlans": "vlans", "zones": "zones", "roles": "server"}
```

- `facets_enabled() -> list[str]`: for each name in `config.ENABLED_FEATURES`, in order, its facet from `FEATURE_FACETS` (skip names not in the table), each facet once.
- `commands(network_os: str) -> list[str]`: `cmds = drivers.core_commands(network_os)`; then each command of `drivers.facet_commands(network_os, facets_enabled())` not already in `cmds` is appended.
- When run as a script with one argument, print `json.dumps(commands(sys.argv[1]))`; with any other number of arguments, `sys.exit("usage: commands.py <ansible_network_os>")`.

Create **`scripts/collect.py`** (`#!/usr/bin/env python3`), which collects one host's output and writes its facts file. Module docstring: what it does, that credentials come from the environment (first match wins: `NET_USERNAME`, `ANSIBLE_NET_USERNAME`, `NAPALM_USERNAME`; `NET_PASSWORD`, `ANSIBLE_NET_PASSWORD`, `NAPALM_PASSWORD`; enable: `NET_ENABLE_PASSWORD`, `ANSIBLE_NET_AUTH_PASS`), that every command is checked against the read-only policy before connecting and again as it is sent, that the config-change marker is read before and after, and the usage:

```text
Usage:
  collect.py --name NAME --host ADDR --platform OS --out FILE
             [--port 22] [--ssh-args "-o KexAlgorithms=+diffie-hellman-group14-sha1"]
             [--timeout 30] [--command-timeout 120]
  collect.py --plan --platform OS     print the commands that would be sent,
                                      checked against the policy, and exit
```

The first paragraph of the docstring (up to the first blank line) is used as the argparse description. Imports: `from __future__ import annotations`, `argparse`, `json`, `os`, `shlex`, `sys`, `from datetime import datetime, timezone`, `from commands import commands`, `import drivers`, `import readonly`, `from sshcli import AuthError, CliError, CliSession`.

```python
USERNAME_VARS = ("NET_USERNAME", "ANSIBLE_NET_USERNAME", "NAPALM_USERNAME")
PASSWORD_VARS = ("NET_PASSWORD", "ANSIBLE_NET_PASSWORD", "NAPALM_PASSWORD")
ENABLE_VARS = ("NET_ENABLE_PASSWORD", "ANSIBLE_NET_AUTH_PASS")
```

- `_env(*names)`: the value of the first variable that is set and non-empty, else `None`.
- `plan(platform: str) -> list[tuple[str, str]]`: every `(kind, command)` collection would send, in order, each checked: `policy = readonly.policy_for(drivers.platform_key(platform))` (raises for a platform with no policy); `check = drivers.config_check_command(platform)`; `steps` = `("session", c)` for each paging command, then `("check", check)` if `check`, then `("collect", c)` for each of `commands(platform)`, then `("check", check)` again if `check`. Call `policy.check(cmd)` for every step (the first refusal raises), then return `steps`.
- `_config_marker(session, command, platform)`: `None` if `command` is falsy. `output = session.run(command, kind="check")`, returning `None` on `CliError`. `None` if the output is blank or `_looks_like_error(output)`. Otherwise `drivers.config_marker(platform, output) or None`; if that raises, `print(f"config-change marker unreadable: {e!r}", file=sys.stderr)` and return `None`.
- `_session(name, host, platform, port, ssh_args, timeout, command_timeout, policy)`: `log = lambda line: print(f"{name}: {line}")`; `username = _env(*USERNAME_VARS)`, `password = _env(*PASSWORD_VARS)`; if either is missing raise `CliError("no credentials found in the environment. In AWX, attach a 'Network' credential to the job template; otherwise set NET_USERNAME/NET_PASSWORD")`. `port = port or 22`; `print(f"{name}: connecting to {host}:{port} as {username!r}")`; return `CliSession(host, username, password, port=port, enable_password=_env(*ENABLE_VARS), ssh_args=list(ssh_args), timeout=timeout, command_timeout=command_timeout, policy=policy, log=log)`.
- `collect(name, host, platform, port=None, ssh_args=(), timeout=30.0, command_timeout=120.0)`:
  1. `policy = readonly.policy_for(drivers.platform_key(platform))`; `plan(platform)` (both raise before anything connects).
  2. `started = datetime.now(timezone.utc).isoformat(timespec="seconds")`; `cli = {}`; `check_cmd = drivers.config_check_command(platform)`.
  3. `with _session(...) as session:` `session.disable_paging(drivers.paging_commands(platform))`; `before = _config_marker(session, check_cmd, platform)`; for each `cmd` of `commands(platform)`: `output = session.run(cmd)`; on `CliError` print `f"{name}: '{cmd}' failed: {e}"` to stderr and continue; if `_looks_like_error(output)` print `f"{name}: '{cmd}' not supported here, skipped"` to stderr and continue; else `cli[cmd] = output`. Then `after = _config_marker(...)`.
  4. `config_check`: `{"status": "unsupported"}` if there is no `check_cmd`; else if `before` or `after` is `None`: `{"status": "unavailable", "command": check_cmd, "before": before, "after": after}` and print `f"{name}: WARNING could not read the config-change marker ({check_cmd!r}), so a config change can't be ruled out"` to stderr; else `{"status": "unchanged" if before == after else "changed", "command": check_cmd, "before": before, "after": after}`.
  5. `record = drivers.build_record(name, platform, cli, collected_at=started)`; `record["audit"] = {"policy": policy.name, "commands": session.audit, "config_check": config_check}`; return it.
- `_looks_like_error(output: str) -> bool`: true if any of the first two lines of `output.strip()` starts (after `lstrip()`) with one of `"% Invalid"`, `"% Unrecognized"`, `"% Unknown"`, `"% Incomplete"`, `"% Ambiguous"`, `"Unrecognized command"`, or, stripped, is exactly `"^"`.
- `main(argv=None)`: argparse with `--plan` (store_true, help `"print the read-only command plan for --platform and exit"`), `--name`, `--host`, `--platform` (required), `--out`, `--port` (int, default `None`, help `"default 22"`), `--ssh-args` (default `""`), `--timeout` (float, 30.0), `--command-timeout` (float, 120.0). If `--plan`: return `print_plan(a.platform)`. If any of `--name`, `--host`, `--out` is missing: `ap.error(f"{', '.join(missing)} required unless --plan")` listing them as `--name` etc. Call `collect(a.name, a.host, a.platform, a.port, shlex.split(a.ssh_args), a.timeout, a.command_timeout)`: on `AuthError` `sys.exit(f"{a.name}: authentication failed - {e}")`; on `CliError` `sys.exit(f"{a.name}: {e}")`; on `readonly.ReadOnlyViolation` print `f"{a.name}: READ-ONLY SAFEGUARD STOPPED COLLECTION: {e}"` to stderr and `sys.exit(3)`. Catch them in that order. Write the record with `json.dump(record, f, indent=2)` (UTF-8). Print `f"{a.name}: collected {len(record['raw'])} command(s): {', '.join(record['raw'])}"`. If the config check status is `"changed"`: print to stderr `f"{a.name}: CONFIGURATION CHANGED DURING COLLECTION. The marker from {check['command']!r} was\n  before: {check['before']}\n  after:  {check['after']}\nCollection only sends commands the read-only policy allows, so check who made this change (it may be another session)."` and `sys.exit(3)`. Otherwise print `f"{a.name}: config-change check: {check['status']}"`.
- `print_plan(platform: str)`: `steps = plan(platform)`; on `readonly.CommandRefused` `sys.exit(f"read-only plan for {platform!r} REFUSED: {e}")`. Print `f"Read-only command plan for platform {platform!r} (policy {policy.name!r}):"`, then `f"  {kind:<8} {cmd}"` per step, then `f"All {len(steps)} commands pass the read-only policy. Login answers, 'enable' and 'exit' are the only other input sent."`.
- Run `main()` under `if __name__ == "__main__":`.

=== END STEP 5 ===

Checks for step 5:

```bash
python3 scripts/collect.py --plan --platform ios
python3 conformance/run_kit.py conformance/stage1/kit.json --section plan --section collect --section helpers --section platforms
```

The plan must print exactly:

```text
Read-only command plan for platform 'ios' (policy 'ios'):
  session  terminal length 0
  check    show running-config | include configuration change
  collect  show version
  collect  show interfaces
  collect  show ip interface
  collect  show lldp neighbors detail
  collect  show cdp neighbors detail
  collect  show ip arp
  collect  show mac address-table dynamic
  collect  show crypto session detail
  collect  show vlan brief
  collect  show interfaces trunk
  check    show running-config | include configuration change
All 13 commands pass the read-only policy. Login answers, 'enable' and 'exit' are the only other input sent.
```

### Step 6: the audit

=== BEGIN STEP 6 ===

Create **`scripts/audit.py`** (`#!/usr/bin/env python3`): after collection, verifies from each facts file's `"audit"` that every command sent was read-only. Imports: `from __future__ import annotations`, `json`, `sys`, `from pathlib import Path`, `import drivers`, `import readonly`. `SESSION_WORDS = ("enable", "exit")`.

- `verify(record: dict) -> tuple[list[str], list[str]]`: returns `(problems, warnings)`.
  1. `audit = record.get("audit")`; if falsy return `(["no audit record: not collected by collect.py with the read-only safeguards, so its commands can't be verified"], [])`.
  2. `policy = readonly.policy_for(drivers.platform_key(_platform(record)))`; on `readonly.CommandRefused` return `([str(e)], [])`.
  3. For `i, entry` in `audit.get("commands", [])`: `cmd = entry.get("command", "")`, `status = entry.get("status")`. If `cmd == "type=keygen"` and `i != 0`, add `"an API login (type=keygen) was sent after the first request"`. If `entry.get("kind") == "session"` and `cmd` is in `SESSION_WORDS`, continue. If not `policy.allows(cmd)`, add `f"command not allowed by the read-only policy: {cmd!r}"`. If `status` is `"refused"` or `"cancelled"`, add `f"{cmd!r} was {status}"`.
  4. `check = audit.get("config_check", {})`, `status = check.get("status")`: `"changed"` → problem `f"configuration changed during collection: {check.get('before')!r} -> {check.get('after')!r}"`; `"unavailable"` → warning `f"config-change marker could not be read ({check.get('command')!r})"`; anything other than `"unchanged"` or `"unsupported"` → problem `f"no config-change check result ({status!r})"`.
- `_platform(record)`: `(record.get("device") or {}).get("platform") or record.get("platform")`.
- `_name(record, default)`: `(record.get("device") or {}).get("name") or record.get("inventory_name") or default`.
- `main(argv=None)`: one argument, the facts directory, else `sys.exit("usage: audit.py <facts_dir>")`. `files = sorted(Path(dir).glob("*.json"))`; none: `sys.exit(f"audit: no facts files in {dir}")`. For each file: load it, `problems, warnings = verify(record)`, `commands = (record.get("audit") or {}).get("commands", [])`, `check = ((record.get("audit") or {}).get("config_check") or {}).get("status", "-")`; print `f"{'FAIL' if problems else 'ok':<5} {name}: {len(commands)} line(s) sent, config {check}"`, then `f"        problem: {p}"` and `f"        warning: {w}"` lines. Finally print `f"audit: {len(files)} host(s), {sent} line(s) sent in total, {failed} failed"` and `sys.exit(1)` if any failed.

=== END STEP 6 ===

Checks for step 6:

```bash
python3 conformance/run_kit.py conformance/stage1/kit.json --section audit
```

### Step 7: the facts checker

=== BEGIN STEP 7 ===

Create **`conformance/check_facts.py`** (`#!/usr/bin/env python3`): checks real facts files against the structure described in the kit, and prints a fingerprint that holds no device data. Messages name a JSON path and a rule, never a value. Imports: `from __future__ import annotations`, `ipaddress`, `json`, `re`, `statistics`, `sys`, `from datetime import datetime`, `from pathlib import Path`, `from run_kit import first_difference, kit_digest`.

```python
_MAC = re.compile(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}")
_VLAN_LIST = re.compile(r"[0-9,-]*")
SESSION_WORDS = ("enable", "exit")
VOLATILE = ("collected_at", "time")
```

- `_kind(v)`: `"null"`, `"bool"`, `"int"`, `"float"`, `"str"`, `"list"`, `"dict"` (test bool before int), else `type(v).__name__`.
- `_format_ok(fmt, value) -> bool`: `"mac"` → `_MAC.fullmatch`; `"ipv4"` → `ipaddress.IPv4Address(value)` succeeds; `"ip"` → `ipaddress.ip_address`; `"ipv4_cidr"` → `"/"` in value and `ipaddress.IPv4Interface(value)` succeeds; `"ipv6_cidr"` → the same with `IPv6Interface`; `"iso_time"` → `datetime.fromisoformat(value).tzinfo is not None`; `"vlan_list"` → `_VLAN_LIST.fullmatch`. A `ValueError` means `False`. Any other format raises `ValueError(f"unknown format {fmt!r} in the kit")`.
- `check_value(spec, value, path, problems)`: `kind = _kind(value)`; if it is not in `spec["type"]`, add `f"{path}: type {kind}, expected {'|'.join(spec['type'])}"` and return. Return if `value is None`. If `"enum"` in spec and the value is not in it: `f"{path}: not one of the allowed values"`. If `"format"` in spec and the value is a `str` and `not _format_ok(...)`: `f"{path}: not a valid {spec['format']}"`. A dict with `"keys"` in spec: for each key of the value not in `spec["keys"]` (in the value's order): `f"{path}: unexpected key {key!r}"`; for each key of `spec["keys"]` (in spec order) missing from the value and not in `spec.get("optional", [])`: `f"{path}: missing key {key!r}"`; if the known keys present, in the value's order, differ from the spec's keys that are present, in spec order: `f"{path}: keys out of order"`; then check each known key present (value's order) with path `f"{path}.{key}"`. A dict with `"values"`: check each item with path `f"{path}[{key!r}]"`. A list with `"items"`: check each with path `f"{path}[{i}]"`.
- `check_record(record, kit, name=None)` → `(problems, warnings)`:
  1. `check_value(kit["facts_schema"], record, "$", problems)`; if there are problems, or the record is not a dict, return now.
  2. If `name is not None` and `device["name"] != name`: `"$.device.name: not the file's name"`. `names` = the interface names; duplicates: `"$.interfaces: duplicate interface names"`.
  3. `plan = kit["plans"].get(device["platform"] or "")`. No `"audit"` key: problem `"$.audit: missing: not collected with the read-only safeguards"`; else if `plan is None`: `"$.device.platform: no plan for this platform in the kit"`; else `_check_audit(audit, plan, problems, warnings)`.
  4. If `plan` is not `None`: `planned` = commands of plan steps whose kind is `"collect"`; `raw = list(record["raw"])`; if any raw key is not planned: `"$.raw: output for a command that is not in the plan"`; elif `raw` differs from `[c for c in planned if c in raw]`: `"$.raw: keys not in plan order"`.
  5. Warnings: `"$.parse_error: the driver could not parse this device"` if the key is present; `"$.interfaces: empty"` if there are none; then for `("$.neighbors[].local_intf", neighbors' local_intf)`, `("$.arp[].intf", ...)`, `("$.mac_table[].intf", ...)`, `("$.interfaces[].parent", ...)` in that order, the count of values that are not `None` and not an interface name, and if non-zero: `f"{path}: {missing} name(s) not among the device's interfaces"`.
- `_check_audit(audit, plan, problems, warnings)`: `lines = [(e["kind"], e["command"]) ...]`. `("session", "enable")` in `lines[1:]` → `"$.audit.commands: 'enable' sent after the first line"`; `("session", "exit")` in `lines[:-1]` → `"$.audit.commands: 'exit' sent before the last line"`. `sent` = lines that aren't a session `enable` or `exit`; `expected` = the plan steps as tuples. If they differ: if some entry's status is `"timeout"` and `sent == expected[:len(sent)]`, warning `"$.audit.commands: stopped early after a timeout"`, else problem `"$.audit.commands: lines sent are not exactly the plan"`. Each entry with status `"refused"` or `"cancelled"`: `f"$.audit.commands[{i}]: {status}"`. Config check status `"changed"`: problem `"$.audit.config_check: configuration changed during collection"`; `"unavailable"`: warning `"$.audit.config_check: config-change marker could not be read"`.
- `_walk(spec, value, path, stats)`: counts per path. `entry = stats.setdefault(path, {})`; count `_kind(value)` in it. Return if the value is `None` or its kind is not in `spec.get("type", [])`. If `"enum"` in spec and the value is a `str`, `bool` or `int`: count `str(value)` in `stats.setdefault(path + " =", {})`. A dict with `"keys"`: walk each spec key present, path `f"{path}.{key}"`. A dict with `"values"`: count each key in `stats.setdefault(path + " keys", {})` and walk each item with path `path + "[*]"`. A list with `"items"`: append `len(value)` to `stats.setdefault(path + " sizes", {}).setdefault("_", [])` and walk each item with path `path + "[]"`.
- `fingerprint(records, kit) -> list[str]`: walk every record from `kit["facts_schema"]` at `"$"`; then for each path in sorted order: a `" sizes"` path gives `f"{path}: total {sum}, per item min {min} median {statistics.median(sizes):g} max {max}"`; any other gives `f"{path}: "` followed by `f"{k}={count}"` for each key in sorted order, joined by spaces.
- `_without_times(value)`: recursively, every dict key in `VOLATILE` gets the value `None`.
- `compare(a, b)`: `first_difference(_without_times(a), _without_times(b))`.
- `main(argv=None)`: `--compare a.json b.json` loads both and prints `"compare: identical apart from collection times"`, or `f"compare: first difference at {path}"` and exits 1. Otherwise two arguments, the kit and the facts directory, else `sys.exit("usage: check_facts.py <kit.json> <facts_dir> | --compare <a.json> <b.json>")`. No `*.json` files: `sys.exit(f"check_facts: no facts files in {dir}")`. Number the files from 1 in sorted order. A file that isn't valid JSON: print `f"FAIL  file {i}: not valid JSON"`. Otherwise `check_record(record, kit, name=path.stem)`; if there are problems or warnings print `f"{'FAIL' if problems else 'warn'}  file {i}"` and the `f"        problem: {p}"` / `f"        warning: {w}"` lines (files are numbered, never named). Then print `f"FINGERPRINT stage {kit['stage']} kit {kit_digest(kit_path)} files {len(files)}"`, each fingerprint line indented by two spaces, and `f"CHECK {'FAIL' if failed else 'PASS'} {len(files)} file(s), {failed} with problems, {warned} with warnings"`. Exit 1 if any file failed.

=== END STEP 7 ===

Checks for step 7:

```bash
python3 conformance/run_kit.py conformance/stage1/kit.json --section check_facts
```

### Step 8: unit tests

=== BEGIN STEP 8 ===

Write these tests with `unittest` only. Each test module starts with `import support` (before project imports) so `scripts/` is importable. `FAKE_SSH = str(support.ROOT / "tests" / "fake_ssh.py")`.

**`tests/test_readonly.py`**:

- `PolicyTest`: every command in an `IOS_ALLOWED` list is returned unchanged by `readonly.policy_for("ios").check()`; every command in an `IOS_REFUSED` list raises `readonly.CommandRefused`. Use these lists:
  - allowed: `"show version"`, `"show interfaces"`, `"show ip interface"`, `"show crypto session detail"`, `"show running-config | include configuration change"`, `"show ip route vrf MGMT"`, `"show interfaces GigabitEthernet1/0/1"`, `"show mac address-table | exclude CPU"`, `"show lldp neighbors detail | section Local Intf"`, `"show logging | count CONFIG_I"`, `"terminal length 0"`, `"terminal width 512"`, `"terminal datadump"`.
  - refused: `"configure terminal"`, `"conf t"`, `"config"`, `"write memory"`, `"wr"`, `"write erase"`, `"copy running-config startup-config"`, `"reload"`, `"reload in 5"`, `"delete flash:x.bin"`, `"erase startup-config"`, `"format flash:"`, `"clear counters"`, `"debug all"`, `"undebug all"`, `"no ip routing"`, `"default interface Gi1"`, `"crypto key zeroize"`, `"license smart register"`, `"install add file x"`, `"request platform"`, `"test crypto"`, `"send log hello"`, `"archive download-sw x"`, `"event manager run x"`, `"tclsh"`, `"boot system flash:x"`, `"monitor capture"`, `"ping 10.0.0.1"`, `"ssh -l a b"`, `"telnet 10.0.0.1"`, `"show running-config | redirect flash:x.txt"`, `"show running-config | tee flash:x"`, `"show running-config | append flash:x"`, `"show version | include a | redirect flash:y"`, `"show version\nconfigure terminal"`, `"show version\rreload"`, `"show version?"`, `"show run\t"`, `"sh run"`, `"show"`, `""`, `"  show version"`, `"show  version"`, `"show version "`, `"terminal length 24 ; reload"`, `"terminal monitor"`, `"terminal length 0\nreload"`, `"set deviceconfig system hostname x"`, `"commit"`, `"Set-NetAdapter -Name x"`, `"Restart-Computer"`.
  - `policy_for` raises `CommandRefused` for `"nxos"`, `"eos"`, `"panos"`, `"windows"`, `""` and `None`; `collect.plan("junos")` raises it too.
- `CommandInventoryTest`: for every platform in `drivers.platforms()`: it has a policy; every paging, core, config check and facet command (`drivers.facet_commands(platform)`, all facets) passes `policy.check`; `collect.plan(platform)` is non-empty. Every command in `drivers._DEFAULT_PAGING` passes the IOS policy.
- `SessionEnforcementTest` (environment `FAKE_SSH_MODE=plain`, `FAKE_SSH_LOG=<temp file>`; sessions `CliSession("sw", "admin", "secret", ssh_binary=FAKE_SSH, timeout=10, command_timeout=10, policy=readonly.policy_for("ios"))`):
  - a refused command (`"configure terminal"`) raises `CommandRefused`, never appears in the device log, and `audit[0]["status"] == "refused"`; `"show version"` afterwards still returns `"SW version    3.0.0.44\n"`;
  - with `policy=None`, `run("show version")` raises `CommandRefused` and nothing but `exit` reaches the device;
  - `"show confirm-test"` raises `readonly.UnexpectedPrompt`, the last audit status is `"cancelled"`, the device log has `"ctrl-c"` and not `"CONFIRMED"`, and the next `run()` raises `CliError` mentioning `"abandoned"` without sending anything;
  - `run("show hang", timeout=1)` raises `CliError` mentioning `"timed out"`, the line after `"show hang"` in the device log is `"ctrl-c"`, and `"show version"` afterwards works.
- `CollectAuditTest`: with `NET_USERNAME=admin`, `NET_PASSWORD=secret`, the fake device (patch `CliSession.__init__` to pass `ssh_binary=FAKE_SSH`) and `collect.commands` patched to return `["show version", "show interfaces status"]`: the audit's commands, apart from the final `"exit"`, equal the device log, which is `["terminal length 0", "show running-config | include configuration change", "show version", "show interfaces status", "show running-config | include configuration change"]`; `audit.verify(record) == ([], [])`. In `FAKE_SSH_MODE=configchange` the config check status is `"changed"` and `audit.verify` reports a problem containing `"configuration changed"`; `collect.main([...])` exits with code 3 and prints `"CONFIGURATION CHANGED"` to stderr. With `"clear counters"` in the command list, `collect.collect` raises `CommandRefused` and the device log file is never created.

**`tests/test_sshcli.py`**: in `FAKE_SSH_MODE=inband`, the prompt is `b"AT-8K-1#"`, `"show interfaces status"` returns the 12 lines `g1  1G-Copper  Full  1000  Up` to `g12 ...` without any pager text, and `"show version"` works afterwards; the same with `FAKE_SSH_SLOW_ERASE=1`; after `disable_paging(["terminal length 0", "terminal datadump"])` the output still has 12 lines; `FAKE_SSH_MODE=plain` works; a wrong password raises `AuthError` whose message doesn't contain the wrong password; `FAKE_SSH_MODE=wrongpass` raises `AuthError`. `collect.collect` with no credential variables set raises `CliError` matching `"Network' credential"`.

**`tests/test_conformance.py`**: runs `run_kit.run(<ROOT>/conformance/stage1/kit.json)` with output captured and asserts it returns `True` (put `conformance/` on `sys.path` first). If it fails, the test message is the captured output.

=== END STEP 8 ===

Checks for step 8:

```bash
python3 -m unittest discover -s tests
```

### Step 9: playbooks

=== BEGIN STEP 9 ===

Create **`playbooks/group_vars/all.yml`**:

```yaml
# Device credentials come from the job's environment, never from this repo.
# In AWX: attach a credential of the built-in "Network" type to the job
# template. It injects ANSIBLE_NET_USERNAME / ANSIBLE_NET_PASSWORD (and
# ANSIBLE_NET_AUTH_PASS for an enable secret). First match wins.
net_username: "{{ lookup('env', 'NET_USERNAME') or lookup('env', 'ANSIBLE_NET_USERNAME') or lookup('env', 'NAPALM_USERNAME') }}"
net_password: "{{ lookup('env', 'NET_PASSWORD') or lookup('env', 'ANSIBLE_NET_PASSWORD') or lookup('env', 'NAPALM_PASSWORD') }}"
net_enable_password: "{{ lookup('env', 'NET_ENABLE_PASSWORD') or lookup('env', 'ANSIBLE_NET_AUTH_PASS') }}"
```

Create **`playbooks/group_vars/network_devices.yml`** with only comments: collect.py does the SSH itself; for older images, `net_ssh_args` can be set, for example `# net_ssh_args: "-o KexAlgorithms=+diffie-hellman-group14-sha1 -o HostKeyAlgorithms=+ssh-rsa"`.

Create **`playbooks/conformance.yml`**: one play on `localhost`, `connection: local`, `gather_facts: no`, which contacts no device. Tasks (all `ansible.builtin`): run `python3 conformance/run_kit.py conformance/stage1/kit.json` with `chdir` set to the repository root (`{{ playbook_dir }}/..`), registered, `changed_when: false`, `failed_when: false`; show its `stdout_lines` with `debug`; run `python3 -m unittest discover -s tests` the same way and show its `stderr_lines`; then two `assert` tasks failing the job if either return code is non-zero.

Create **`playbooks/gather_topology.yml`** with four plays, in this order:

1. **"Check the code and the read-only command plan"** on `localhost`, `connection: local`, `gather_facts: no`: create `{{ playbook_dir }}/../output/facts` (`file`, `state: directory`, `mode: "0755"`); run the conformance kit (`python3 conformance/run_kit.py conformance/stage1/kit.json`, `chdir: "{{ playbook_dir }}/.."`, `changed_when: false`), so the job stops before contacting any device if the code on this execution environment doesn't pass it; then, with `chdir: "{{ playbook_dir }}/../scripts"`, run `python3 collect.py --plan --platform {{ item }}` looping over `{{ groups['network_devices'] | map('extract', hostvars, 'ansible_network_os') | unique | list }}`, registered as `command_plan`, `changed_when: false` (a refused plan fails the job here); and show `{{ command_plan.results | map(attribute='stdout_lines') | list }}` with `debug`.
2. **"Gather show-command output from all inventory devices"** on `network_devices`, `connection: local`, `gather_facts: no`, `vars: ansible_python_interpreter: "{{ ansible_playbook_python }}"`. Ansible itself never connects to the devices. Tasks: assert `net_username | length > 0 and net_password | length > 0` (`quiet: true`, `fail_msg` telling the user to attach a credential of the built-in "Network" type, or export NET_USERNAME and NET_PASSWORD); then the `command`:
   ```yaml
   command: >-
     python3 {{ playbook_dir }}/../scripts/collect.py
     --name {{ inventory_hostname }}
     --host {{ ansible_host | default(inventory_hostname) }}
     {{ ('--port ' ~ ansible_port) if ansible_port is defined else '' }}
     --platform {{ ansible_network_os }}
     --out {{ playbook_dir }}/../output/facts/{{ inventory_hostname }}.json
     --ssh-args {{ net_ssh_args | default('') | quote }}
   environment:
     NET_USERNAME: "{{ net_username }}"
     NET_PASSWORD: "{{ net_password }}"
     NET_ENABLE_PASSWORD: "{{ net_enable_password }}"
   register: collect_result
   changed_when: false
   ```
   and `debug: var: collect_result.stdout_lines`. Credentials go only through `environment`, never on the command line.
3. **"Push all gathered facts to artifact server"** on `artifact_server`, `gather_facts: no`: `ansible.builtin.copy` from `{{ playbook_dir }}/../output/facts/` to `{{ artifact_facts_dir | default('/home/hp/Projects/autotop-awx/output/facts/') }}` with `mode: "0755"`. (No hosts in `artifact_server` means this play is skipped.)
4. **"Verify every command sent was read-only"** on `localhost`, `connection: local`, `gather_facts: no`: run `python3 {{ playbook_dir }}/../scripts/audit.py {{ playbook_dir }}/../output/facts` registered as `audit_result`, and `python3 {{ playbook_dir }}/../conformance/check_facts.py {{ playbook_dir }}/../conformance/stage1/kit.json {{ playbook_dir }}/../output/facts` registered as `check_result`, both `changed_when: false` and `failed_when: false`; show both `stdout_lines`; then assert `audit_result.rc == 0` (`fail_msg: "Read-only audit failed - see the audit output above."`) and `check_result.rc == 0` (`fail_msg: "The facts files don't match the reference structure - see the check output above."`).

Use only `ansible.builtin` modules (`file`, `command`, `debug`, `assert`, `copy`).

=== END STEP 9 ===

Checks for step 9: `ansible-playbook --syntax-check -i inventory.yml playbooks/gather_topology.yml` and the same for `playbooks/conformance.yml`. Then run `playbooks/conformance.yml` as an AWX job: it contacts nothing.

Example `inventory.yml` (yours will list production devices; keep `ansible_network_os: ios`):

```yaml
all:
  children:
    network_devices:
      hosts:
        canary-sw1:
          ansible_host: 10.0.0.1
          ansible_network_os: ios
```

### Step 10: acceptance, then the first live run

1. **Static review of the read-only path** (by you, not Copilot). Each must hold:
   - `grep -n "os.write\|_send(\|execvp\|pty.fork" scripts/*.py scripts/drivers/*.py` finds lines only in `scripts/sshcli.py`, inside `_spawn`, `_send`, `open`, `_enable`, `run`, `_cancel` and `close`. (The kit's `structure` section checks the same thing automatically.)
   - `grep -rn "subprocess\|os.system\|popen\|paramiko\|netmiko\|pexpect\|socket" scripts/` finds nothing.
   - In `CliSession.run`, `self.policy.check(command)` comes before `self._record(command, kind, "sent")` and `self._send(command)`.
   - `scripts/readonly.py` has exactly one policy, `"ios"`.
2. **Offline gate:** the AWX job for `playbooks/conformance.yml` ends with `RESULT PASS 859/859 kit 9d80860f96e9ccca` and all unit tests OK. Send me those two lines.
3. **The plan:** the first play of `gather_topology.yml` prints exactly the plan shown under step 5.
4. **First live run, one canary device** (a switch you could lose for an hour). Before it, note the device's `show running-config | include configuration change` line and, if you have command accounting (TACACS+ or `archive log config`), its position. Run `gather_topology.yml` limited to that host (`--limit canary-sw1` / the job template's Limit field). Then confirm: the audit line is `ok`, `CHECK PASS 1 file(s)`, the config check is `unchanged`, the device's own accounting shows exactly the lines in the job log (`sent  ...`) and nothing else. Send me the audit summary line, the `CHECK` line and the `FINGERPRINT` block.
5. Widen to a handful of devices covering each model and IOS family, then to all IOS devices, sending the same lines each time. The [validation method](production-rebuild-validation.md) describes each gate.
