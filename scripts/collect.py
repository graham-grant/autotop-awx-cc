#!/usr/bin/env python3
"""
Collects one host's show-command output over SSH and writes the raw-facts
JSON main.py reads:

    {"inventory_name": ..., "platform": ..., "cli": {command: output}}

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

Usage:
  collect.py --name NAME --host ADDR --platform OS --out FILE
             [--port 22] [--ssh-args "-o KexAlgorithms=+diffie-hellman-group14-sha1"]
             [--timeout 30] [--command-timeout 120]

A command that fails (e.g. not supported on this box) is left out of
"cli"; failing to log in at all exits non-zero.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys

from commands import commands
import netparse
from sshcli import AuthError, CliError, CliSession


USERNAME_VARS = ("NET_USERNAME", "ANSIBLE_NET_USERNAME", "NAPALM_USERNAME")
PASSWORD_VARS = ("NET_PASSWORD", "ANSIBLE_NET_PASSWORD", "NAPALM_PASSWORD")
ENABLE_VARS = ("NET_ENABLE_PASSWORD", "ANSIBLE_NET_AUTH_PASS")


def _env(*names):
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    return None


def collect(name, host, platform, port=22, ssh_args=(), timeout=30.0, command_timeout=120.0):
    username = _env(*USERNAME_VARS)
    password = _env(*PASSWORD_VARS)
    if not username or not password:
        raise CliError("no credentials found in the environment. In AWX, attach a "
                       "'Network' credential to the job template; otherwise set "
                       "NET_USERNAME/NET_PASSWORD")
    print(f"{name}: connecting to {host}:{port} as {username!r}")
    cli = {}
    with CliSession(host, username, password, port=port,
                    enable_password=_env(*ENABLE_VARS),
                    ssh_args=list(ssh_args), timeout=timeout,
                    command_timeout=command_timeout) as session:
        session.disable_paging(netparse.paging_commands(platform))
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
    return {"inventory_name": name, "platform": platform, "cli": cli}


def _looks_like_error(output: str) -> bool:
    first = output.strip().splitlines()[:2]
    return any(l.lstrip().startswith(("% Invalid", "% Unrecognized", "% Unknown",
                                      "% Incomplete", "% Ambiguous", "Unrecognized command"))
               or l.strip() == "^" for l in first)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--name", required=True)
    ap.add_argument("--host", required=True)
    ap.add_argument("--platform", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--port", type=int, default=22)
    ap.add_argument("--ssh-args", default="")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--command-timeout", type=float, default=120.0)
    a = ap.parse_args(argv)
    try:
        record = collect(a.name, a.host, a.platform, a.port, shlex.split(a.ssh_args),
                         a.timeout, a.command_timeout)
    except AuthError as e:
        sys.exit(f"{a.name}: authentication failed - {e}")
    except CliError as e:
        sys.exit(f"{a.name}: {e}")
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    print(f"{a.name}: collected {len(record['cli'])} command(s): {', '.join(record['cli'])}")


if __name__ == "__main__":
    main()
