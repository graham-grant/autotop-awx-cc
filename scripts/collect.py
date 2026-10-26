#!/usr/bin/env python3
"""
Collects one host's show-command output over SSH and writes its facts
file: a schema 2 record (see scripts/drivers/__init__.py) with the parsed
core and facets, the raw output under "raw", and the audit of every line
sent under "audit".

Run by the playbook on the controller, once per inventory host. Uses
sshcli.CliSession (system ssh + pty, standard library only): one
interactive session per device, like NAPALM had, reading credentials
straight from the environment rather than through Ansible's connection
variables, and answering password, in-session login, pager and "enable"
prompts. On a failed login it prints what the device actually said.

Credentials come from the environment, never the command line, first
match wins:
  username:  NET_USERNAME, ANSIBLE_NET_USERNAME, NAPALM_USERNAME
  password:  NET_PASSWORD, ANSIBLE_NET_PASSWORD, NAPALM_PASSWORD
  enable:    NET_ENABLE_PASSWORD, ANSIBLE_NET_AUTH_PASS  (optional, used if
             the device lands at a ">" prompt)
ANSIBLE_NET_* are what AWX's built-in "Network" credential type injects.

Read-only: collection must never change a device. Before connecting, every
command in the plan is checked against the platform's policy
(scripts/readonly.py), and a platform without one is never contacted.
sshcli checks each command again as it is sent, cancels any confirmation
prompt instead of answering it, and records everything typed; that record
goes in the JSON as "audit" and in the job log. Where the platform has a
config-change marker (IOS: "Last configuration change at ..."), it is read
before and after, and a change fails the host loudly.

Usage:
  collect.py --name NAME --host ADDR --platform OS --out FILE
             [--port 22] [--ssh-args "-o KexAlgorithms=+diffie-hellman-group14-sha1"]
             [--timeout 30] [--command-timeout 120]
  collect.py --plan --platform OS     print the commands that would be sent,
                                      checked against the policy, and exit

A command that fails (e.g. not supported on this box) is left out of
"raw"; failing to log in at all exits non-zero, and so does anything the
read-only checks stop (exit 3).
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from datetime import datetime, timezone

from commands import commands
import drivers
import readonly
from sshcli import AuthError, CliError, CliSession


USERNAME_VARS = ("NET_USERNAME", "ANSIBLE_NET_USERNAME", "NAPALM_USERNAME")
PASSWORD_VARS = ("NET_PASSWORD", "ANSIBLE_NET_PASSWORD", "NAPALM_PASSWORD")
ENABLE_VARS = ("NET_ENABLE_PASSWORD", "ANSIBLE_NET_AUTH_PASS")


def _env(*names):
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    return None


def plan(platform: str) -> list[tuple[str, str]]:
    """Every (kind, command) collection would send to a host of this
    platform, in order, each checked against the read-only policy. Raises
    readonly.CommandRefused if any would be refused, or if the platform has
    no policy - before anything connects."""
    policy = readonly.policy_for(drivers.platform_key(platform))
    check = drivers.config_check_command(platform)
    steps = [("session", c) for c in drivers.paging_commands(platform)]
    steps += [("check", check)] if check else []
    steps += [("collect", c) for c in commands(platform)]
    steps += [("check", check)] if check else []
    for _, cmd in steps:
        policy.check(cmd)
    return steps


def _config_marker(session, command):
    if not command:
        return None
    try:
        output = session.run(command, kind="check")
    except CliError:
        return None
    marker = output.strip()
    return None if not marker or _looks_like_error(output) else marker


def collect(name, host, platform, port=22, ssh_args=(), timeout=30.0, command_timeout=120.0):
    policy = readonly.policy_for(drivers.platform_key(platform))
    plan(platform)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    username = _env(*USERNAME_VARS)
    password = _env(*PASSWORD_VARS)
    if not username or not password:
        raise CliError("no credentials found in the environment. In AWX, attach a "
                       "'Network' credential to the job template; otherwise set "
                       "NET_USERNAME/NET_PASSWORD")
    print(f"{name}: connecting to {host}:{port} as {username!r}")
    cli = {}
    check_cmd = drivers.config_check_command(platform)
    with CliSession(host, username, password, port=port,
                    enable_password=_env(*ENABLE_VARS),
                    ssh_args=list(ssh_args), timeout=timeout,
                    command_timeout=command_timeout, policy=policy,
                    log=lambda line: print(f"{name}: {line}")) as session:
        session.disable_paging(drivers.paging_commands(platform))
        before = _config_marker(session, check_cmd)
        for cmd in commands(platform):
            try:
                output = session.run(cmd)
            except CliError as e:
                print(f"{name}: '{cmd}' failed: {e}", file=sys.stderr)
                continue
            if _looks_like_error(output):
                print(f"{name}: '{cmd}' not supported here, skipped", file=sys.stderr)
                continue
            cli[cmd] = output
        after = _config_marker(session, check_cmd)
    if not check_cmd:
        config_check = {"status": "unsupported"}
    elif before is None or after is None:
        config_check = {"status": "unavailable", "command": check_cmd,
                        "before": before, "after": after}
        print(f"{name}: WARNING could not read the config-change marker "
              f"({check_cmd!r}), so a config change can't be ruled out", file=sys.stderr)
    else:
        config_check = {"status": "unchanged" if before == after else "changed",
                        "command": check_cmd, "before": before, "after": after}
    record = drivers.build_record(name, platform, cli, collected_at=started)
    record["audit"] = {"policy": policy.name, "commands": session.audit,
                       "config_check": config_check}
    return record


def _looks_like_error(output: str) -> bool:
    first = output.strip().splitlines()[:2]
    return any(l.lstrip().startswith(("% Invalid", "% Unrecognized", "% Unknown",
                                      "% Incomplete", "% Ambiguous", "Unrecognized command"))
               or l.strip() == "^" for l in first)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--plan", action="store_true",
                    help="print the read-only command plan for --platform and exit")
    ap.add_argument("--name")
    ap.add_argument("--host")
    ap.add_argument("--platform", required=True)
    ap.add_argument("--out")
    ap.add_argument("--port", type=int, default=22)
    ap.add_argument("--ssh-args", default="")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--command-timeout", type=float, default=120.0)
    a = ap.parse_args(argv)
    if a.plan:
        return print_plan(a.platform)
    missing = [f"--{n}" for n in ("name", "host", "out") if not getattr(a, n)]
    if missing:
        ap.error(f"{', '.join(missing)} required unless --plan")
    try:
        record = collect(a.name, a.host, a.platform, a.port, shlex.split(a.ssh_args),
                         a.timeout, a.command_timeout)
    except AuthError as e:
        sys.exit(f"{a.name}: authentication failed - {e}")
    except CliError as e:
        sys.exit(f"{a.name}: {e}")
    except readonly.ReadOnlyViolation as e:
        print(f"{a.name}: READ-ONLY SAFEGUARD STOPPED COLLECTION: {e}", file=sys.stderr)
        sys.exit(3)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    print(f"{a.name}: collected {len(record['raw'])} command(s): {', '.join(record['raw'])}")
    check = record["audit"]["config_check"]
    if check["status"] == "changed":
        print(f"{a.name}: CONFIGURATION CHANGED DURING COLLECTION. The marker from "
              f"{check['command']!r} was\n  before: {check['before']}\n  after:  "
              f"{check['after']}\nCollection only sends commands the read-only policy "
              "allows, so check who made this change (it may be another session).",
              file=sys.stderr)
        sys.exit(3)
    print(f"{a.name}: config-change check: {check['status']}")


def print_plan(platform: str):
    try:
        steps = plan(platform)
    except readonly.CommandRefused as e:
        sys.exit(f"read-only plan for {platform!r} REFUSED: {e}")
    policy = readonly.policy_for(drivers.platform_key(platform))
    print(f"Read-only command plan for platform {platform!r} (policy {policy.name!r}):")
    for kind, cmd in steps:
        print(f"  {kind:<8} {cmd}")
    print(f"All {len(steps)} commands pass the read-only policy. Login answers, "
          "'enable' and 'exit' are the only other input sent.")


if __name__ == "__main__":
    main()
