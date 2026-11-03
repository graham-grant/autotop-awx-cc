#!/usr/bin/env python3
"""
Checks real facts files (output/facts/*.json) against the structure the
reference implementation writes, and prints a fingerprint of them that
holds no device data, so it can be read out of a segregated environment.

The structure comes from the conformance kit ("facts_schema" and "plans"),
not from this file, so the same checker works for every stage.

For each file it checks:
  - every key, type, allowed value and format against facts_schema, and
    the order of every object's keys (the reference writes them in a fixed
    order, so a file written by conforming code has the same bytes);
  - the device name is the file name, and interface names are unique;
  - the audit: present, every line sent is exactly the platform's plan in
    order ("enable" may come first, "exit" last), nothing refused or
    cancelled, and the configuration did not change;
  - raw output only for planned collect commands, in plan order;
and warns about a parse_error, a device with no interfaces, an unreadable
config-change marker, and neighbor, ARP, MAC-table or parent interface
names that don't match one of the device's own interfaces.

Messages name a JSON path and a rule, never a value, so they are safe to
share. Exit status 1 if any file has a problem.

Usage:
  check_facts.py <kit.json> <facts_dir>        check every file, then print
                                               the fingerprint
  check_facts.py --compare <a.json> <b.json>   compare two facts files of the
                                               same device, ignoring times
                                               (for a lab run of both
                                               implementations)
"""
from __future__ import annotations

import ipaddress
import json
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

from run_kit import first_difference, kit_digest

_MAC = re.compile(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}")
_VLAN_LIST = re.compile(r"[0-9,-]*")
SESSION_WORDS = ("enable", "exit")


def _kind(v):
    return ("null" if v is None else "bool" if isinstance(v, bool) else
            "int" if isinstance(v, int) else "float" if isinstance(v, float) else
            "str" if isinstance(v, str) else "list" if isinstance(v, list) else
            "dict" if isinstance(v, dict) else type(v).__name__)


def _format_ok(fmt, value) -> bool:
    try:
        if fmt == "mac":
            return bool(_MAC.fullmatch(value))
        if fmt == "ipv4":
            ipaddress.IPv4Address(value)
            return True
        if fmt == "ip":
            ipaddress.ip_address(value)
            return True
        if fmt == "ipv4_cidr":
            return "/" in value and bool(ipaddress.IPv4Interface(value))
        if fmt == "ipv6_cidr":
            return "/" in value and bool(ipaddress.IPv6Interface(value))
        if fmt == "iso_time":
            return datetime.fromisoformat(value).tzinfo is not None
        if fmt == "vlan_list":
            return bool(_VLAN_LIST.fullmatch(value))
    except ValueError:
        return False
    raise ValueError(f"unknown format {fmt!r} in the kit")


def check_value(spec, value, path, problems):
    """Check one value against its schema spec, recursively. Specs are
    {"type": [...]} plus "enum", "format", "keys" + "optional" (an object
    with known keys, in order), "values" (an object with free keys) or
    "items" (a list)."""
    kind = _kind(value)
    if kind not in spec["type"]:
        problems.append(f"{path}: type {kind}, expected {'|'.join(spec['type'])}")
        return
    if value is None:
        return
    if "enum" in spec and value not in spec["enum"]:
        problems.append(f"{path}: not one of the allowed values")
    if "format" in spec and isinstance(value, str) and not _format_ok(spec["format"], value):
        problems.append(f"{path}: not a valid {spec['format']}")
    if kind == "dict" and "keys" in spec:
        known = list(spec["keys"])
        for key in value:
            if key not in spec["keys"]:
                problems.append(f"{path}: unexpected key {key!r}")
        for key in known:
            if key not in value and key not in spec.get("optional", []):
                problems.append(f"{path}: missing key {key!r}")
        order = [k for k in value if k in spec["keys"]]
        if order != [k for k in known if k in value]:
            problems.append(f"{path}: keys out of order")
        for key in order:
            check_value(spec["keys"][key], value[key], f"{path}.{key}", problems)
    elif kind == "dict" and "values" in spec:
        for key, item in value.items():
            check_value(spec["values"], item, f"{path}[{key!r}]", problems)
    elif kind == "list" and "items" in spec:
        for i, item in enumerate(value):
            check_value(spec["items"], item, f"{path}[{i}]", problems)


def check_record(record, kit, name=None):
    """(problems, warnings) for one facts record."""
    problems, warnings = [], []
    check_value(kit["facts_schema"], record, "$", problems)
    if problems or not isinstance(record, dict):
        return problems, warnings
    device = record["device"]
    if name is not None and device["name"] != name:
        problems.append("$.device.name: not the file's name")
    names = [i["name"] for i in record["interfaces"]]
    if len(set(names)) != len(names):
        problems.append("$.interfaces: duplicate interface names")
    plan = kit["plans"].get(device["platform"] or "")
    audit = record.get("audit")
    if audit is None:
        problems.append("$.audit: missing: not collected with the read-only safeguards")
    elif plan is None:
        problems.append("$.device.platform: no plan for this platform in the kit")
    else:
        _check_audit(audit, plan, problems, warnings)
    if plan is not None:
        planned = [c for k, c in plan if k == "collect"]
        raw = list(record["raw"])
        if any(c not in planned for c in raw):
            problems.append("$.raw: output for a command that is not in the plan")
        elif raw != [c for c in planned if c in raw]:
            problems.append("$.raw: keys not in plan order")
    if "parse_error" in record:
        warnings.append("$.parse_error: the driver could not parse this device")
    if not record["interfaces"]:
        warnings.append("$.interfaces: empty")
    known = set(names)
    for path, values in (("$.neighbors[].local_intf", [n["local_intf"] for n in record["neighbors"]]),
                         ("$.arp[].intf", [a["intf"] for a in record["arp"]]),
                         ("$.mac_table[].intf", [m["intf"] for m in record["mac_table"]]),
                         ("$.interfaces[].parent", [i["parent"] for i in record["interfaces"]])):
        missing = sum(1 for v in values if v is not None and v not in known)
        if missing:
            warnings.append(f"{path}: {missing} name(s) not among the device's interfaces")
    return problems, warnings


def _check_audit(audit, plan, problems, warnings):
    entries = audit["commands"]
    lines = [(e["kind"], e["command"]) for e in entries]
    if ("session", "enable") in lines[1:]:
        problems.append("$.audit.commands: 'enable' sent after the first line")
    if ("session", "exit") in lines[:-1]:
        problems.append("$.audit.commands: 'exit' sent before the last line")
    sent = [line for line in lines
            if not (line[0] == "session" and line[1] in SESSION_WORDS)]
    expected = [tuple(step) for step in plan]
    if sent != expected:
        timed_out = any(e["status"] == "timeout" for e in entries)
        if timed_out and sent == expected[:len(sent)]:
            warnings.append("$.audit.commands: stopped early after a timeout")
        else:
            problems.append("$.audit.commands: lines sent are not exactly the plan")
    for i, e in enumerate(entries):
        if e["status"] in ("refused", "cancelled"):
            problems.append(f"$.audit.commands[{i}]: {e['status']}")
    status = audit["config_check"]["status"]
    if status == "changed":
        problems.append("$.audit.config_check: configuration changed during collection")
    elif status == "unavailable":
        warnings.append("$.audit.config_check: config-change marker could not be read")


# -- fingerprint ------------------------------------------------------------------

def _walk(spec, value, path, stats):
    """Count, per schema path, the types seen, and for enums the values
    seen. Free-key objects count their keys. No other value is kept."""
    entry = stats.setdefault(path, {})
    kind = _kind(value)
    entry[kind] = entry.get(kind, 0) + 1
    if value is None or kind not in spec.get("type", []):
        return
    if "enum" in spec and isinstance(value, (str, bool, int)):
        values = stats.setdefault(path + " =", {})
        values[str(value)] = values.get(str(value), 0) + 1
    if kind == "dict" and "keys" in spec:
        for key in spec["keys"]:
            if key in value:
                _walk(spec["keys"][key], value[key], f"{path}.{key}", stats)
    elif kind == "dict" and "values" in spec:
        keys = stats.setdefault(path + " keys", {})
        for key, item in value.items():
            keys[key] = keys.get(key, 0) + 1
            _walk(spec["values"], item, path + "[*]", stats)
    elif kind == "list" and "items" in spec:
        sizes = stats.setdefault(path + " sizes", {})
        sizes.setdefault("_", []).append(len(value))
        for item in value:
            _walk(spec["items"], item, path + "[]", stats)


def fingerprint(records, kit):
    """Lines describing the shape of a set of facts records: counts, types
    and allowed values only."""
    stats = {}
    for record in records:
        _walk(kit["facts_schema"], record, "$", stats)
    lines = []
    for path in sorted(stats):
        counts = stats[path]
        if path.endswith(" sizes"):
            sizes = counts["_"]
            lines.append(f"{path}: total {sum(sizes)}, per item min {min(sizes)} "
                         f"median {statistics.median(sizes):g} max {max(sizes)}")
        else:
            lines.append(f"{path}: " + " ".join(f"{k}={counts[k]}" for k in sorted(counts)))
    return lines


# -- compare two runs ------------------------------------------------------------

VOLATILE = ("collected_at", "time")


def _without_times(value):
    if isinstance(value, dict):
        return {k: (None if k in VOLATILE else _without_times(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_without_times(v) for v in value]
    return value


def compare(a, b):
    """The first difference between two facts records, times ignored, as a
    (path, expected, actual) tuple, or None."""
    return first_difference(_without_times(a), _without_times(b))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 3 and argv[0] == "--compare":
        a, b = (json.loads(Path(p).read_text(encoding="utf-8")) for p in argv[1:])
        diff = compare(a, b)
        if diff is None:
            print("compare: identical apart from collection times")
            return
        print(f"compare: first difference at {diff[0]}")
        sys.exit(1)
    if len(argv) != 2:
        sys.exit("usage: check_facts.py <kit.json> <facts_dir> | --compare <a.json> <b.json>")
    kit = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    files = sorted(Path(argv[1]).glob("*.json"))
    if not files:
        sys.exit(f"check_facts: no facts files in {argv[1]}")
    records, failed, warned = [], 0, 0
    for i, path in enumerate(files, 1):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            print(f"FAIL  file {i}: not valid JSON")
            failed += 1
            continue
        problems, warnings = check_record(record, kit, name=path.stem)
        records.append(record)
        failed += bool(problems)
        warned += bool(warnings)
        if problems or warnings:
            # files are numbered, not named: device names stay inside
            print(f"{'FAIL' if problems else 'warn'}  file {i}")
            for p in problems:
                print(f"        problem: {p}")
            for w in warnings:
                print(f"        warning: {w}")
    print(f"FINGERPRINT stage {kit['stage']} kit {kit_digest(argv[0])} files {len(files)}")
    for line in fingerprint(records, kit):
        print(f"  {line}")
    print(f"CHECK {'FAIL' if failed else 'PASS'} {len(files)} file(s), {failed} with problems, "
          f"{warned} with warnings")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
