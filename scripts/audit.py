#!/usr/bin/env python3
"""
Verifies, after collection, that every command sent to every device was
read-only, from the "audit" section collect.py writes into each facts file.

For each output/facts/<host>.json it checks that:
  - the file has an audit (it was collected with the read-only safeguards);
  - every command recorded was allowed by the platform's read-only policy
    (re-checked here, independently of the check made when it was sent),
    apart from the fixed session words "enable" and "exit";
  - nothing was refused or cancelled;
  - the config-change marker did not move (or says why it couldn't be read).

Prints one line per host and a summary, and exits 1 if any host fails, so
the AWX job is marked failed. "unavailable" config checks are warnings.

Usage: python3 audit.py <facts_dir>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import netparse
import readonly

SESSION_WORDS = ("enable", "exit")


def verify(record: dict) -> tuple[list[str], list[str]]:
    """Return (problems, warnings) for one facts record."""
    problems, warnings = [], []
    audit = record.get("audit")
    if not audit:
        return ["no audit record: not collected by collect.py with the read-only "
                "safeguards, so its commands can't be verified"], warnings
    try:
        policy = readonly.policy_for(netparse.platform_key(record.get("platform")))
    except readonly.CommandRefused as e:
        return [str(e)], warnings
    for entry in audit.get("commands", []):
        cmd, status = entry.get("command", ""), entry.get("status")
        if entry.get("kind") == "session" and cmd in SESSION_WORDS:
            continue
        if not policy.allows(cmd):
            problems.append(f"command not allowed by the read-only policy: {cmd!r}")
        if status in ("refused", "cancelled"):
            problems.append(f"{cmd!r} was {status}")
    check = audit.get("config_check", {})
    status = check.get("status")
    if status == "changed":
        problems.append(f"configuration changed during collection: "
                        f"{check.get('before')!r} -> {check.get('after')!r}")
    elif status == "unavailable":
        warnings.append(f"config-change marker could not be read ({check.get('command')!r})")
    elif status not in ("unchanged", "unsupported"):
        problems.append(f"no config-change check result ({status!r})")
    return problems, warnings


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        sys.exit("usage: audit.py <facts_dir>")
    files = sorted(Path(argv[0]).glob("*.json"))
    if not files:
        sys.exit(f"audit: no facts files in {argv[0]}")
    failed = 0
    sent = 0
    for path in files:
        record = json.loads(path.read_text(encoding="utf-8"))
        name = record.get("inventory_name", path.stem)
        problems, warnings = verify(record)
        commands = (record.get("audit") or {}).get("commands", [])
        sent += len(commands)
        check = ((record.get("audit") or {}).get("config_check") or {}).get("status", "-")
        print(f"{'FAIL' if problems else 'ok':<5} {name}: {len(commands)} line(s) sent, "
              f"config {check}")
        for p in problems:
            print(f"        problem: {p}")
        for w in warnings:
            print(f"        warning: {w}")
        failed += bool(problems)
    print(f"audit: {len(files)} host(s), {sent} line(s) sent in total, {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
