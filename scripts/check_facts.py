#!/usr/bin/env python3
"""
Checks the facts files collect.py wrote: that each one is valid JSON in the
common device format (schema 2, see scripts/drivers/__init__.py), with the
right keys, types and allowed values, and that what was sent and kept
matches the read-only plan. It reads files only and never contacts a
device.

For each file it checks:
  - every key, type, allowed value and format in FACTS_SCHEMA below (MAC
    addresses written aa:bb:cc:dd:ee:ff, addresses and prefixes that parse,
    timestamps with a time zone);
  - the device name is the file name, and interface names are unique;
  - raw output is kept only for commands in the platform's plan
    (collect.plan()), in plan order;
  - the audit is present, nothing was refused or cancelled, and the
    configuration did not change. For SSH (Cisco) hosts, the lines sent
    are exactly the plan, in order, with only "enable" first and "exit"
    last added; a timeout that stopped the run early is a warning.
It warns about a parse_error, a device with no interfaces, a planned
command with no output kept (the device doesn't support it), empty
output (nothing configured, such as CDP with no neighbors), an unreadable
config-change marker, and neighbor, ARP, MAC table or parent interface
names that aren't one of the device's own interfaces.

Prints one line per file with what was found (model, software, counts), any
problems and warnings, then a CHECK line. Exits 1 if any file has a
problem, so the AWX job fails.

Usage:
  check_facts.py <facts_dir>          check every *.json file in the folder
  check_facts.py <file.json> ...      check these files
"""
from __future__ import annotations

import ipaddress
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import collect
import drivers
import readonly

_MAC = re.compile(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}")
_VLAN_LIST = re.compile(r"[0-9,-]*")
SESSION_WORDS = ("enable", "exit")


def _t(*types, **rest):
    return {"type": list(types), **rest}


def _obj(keys, optional=()):
    return {"type": ["dict"], "keys": keys, "optional": list(optional)}


def _list(item):
    return {"type": ["list"], "items": item}


STR, OPT_STR = _t("str"), _t("str", "null")
OPT_BOOL, OPT_INT = _t("bool", "null"), _t("int", "null")

# The schema 2 record, as the drivers write it. Facets not listed here are
# reported as a warning, not checked.
FACETS = {
    "ipsec": _obj({"tunnels": _list(_obj({
        "interface": STR, "local_address": OPT_STR, "peer_address": STR,
        "name": STR, "status": _t("str", enum=["up", "down", "unknown"]),
        "vendor_status": OPT_STR}, optional=["name"]))}),
    "vlans": _obj({
        "vlans": _list(_obj({"id": _t("int"), "name": STR, "ports": _list(STR)})),
        "trunks": _list(_obj({"port": STR, "native": OPT_INT,
                              "allowed": _t("str", format="vlan_list"),
                              "active": _t("str", format="vlan_list")}))}),
    "zones": _obj({"zones": _list(_obj({"name": STR, "vsys": OPT_STR,
                                        "interfaces": _list(STR)}))}),
    "server": _obj({"domain": OPT_STR,
                    "roles": _list(_obj({"name": STR, "display_name": OPT_STR}))},
                   optional=["domain", "roles"]),
}

FACTS_SCHEMA = _obj({
    "schema": _t("int", enum=[drivers.SCHEMA]),
    "device": _obj({
        "name": STR, "hostname": STR,
        "role": _t("str", "null", enum=["router", "switch", "firewall", "server"]),
        "vendor": OPT_STR,
        "platform": _t("str", "null", enum=drivers.platforms()),
        "model": OPT_STR, "os_version": OPT_STR, "serial": OPT_STR,
        "collected_at": _t("str", "null", format="iso_time"),
        "collector": _t("str", "null", enum=["ssh-cli", "xml-api", "powershell-ssh"]),
    }),
    "interfaces": _list(_obj({
        "name": STR,
        "kind": _t("str", enum=["physical", "subinterface", "lag", "vlan",
                                "loopback", "tunnel", "virtual"]),
        "mac": _t("str", "null", format="mac"),
        "admin_up": OPT_BOOL, "oper_up": OPT_BOOL,
        "speed_mbps": _t("int", "float", "null"), "mtu": OPT_INT,
        "description": STR, "vrf": OPT_STR, "vlan": OPT_INT, "parent": OPT_STR,
        "ipv4": _list(_t("str", format="ipv4_cidr")),
        "ipv6": _list(_t("str", format="ipv6_cidr")),
    })),
    "neighbors": _list(_obj({
        "local_intf": STR, "protocol": _t("str", enum=["lldp", "cdp"]),
        "remote_name": OPT_STR, "remote_intf": OPT_STR,
        "remote_mac": _t("str", "null", format="mac"),
        "remote_ip": OPT_STR, "remote_platform": OPT_STR,
        "remote_capabilities": _list(_t("str", enum=[
            "router", "bridge", "phone", "wlan", "station", "docsis", "other",
            "repeater"])),
    })),
    "arp": _list(_obj({"ip": _t("str", format="ip"),
                       "mac": _t("str", "null", format="mac"),
                       "intf": OPT_STR, "vrf": STR})),
    "mac_table": _list(_obj({"mac": _t("str", format="mac"), "intf": STR,
                             "vlan": OPT_INT})),
    "facets": {"type": ["dict"], "facets": True},
    "raw": {"type": ["dict"], "values": STR},
    "parse_error": STR,
    "audit": _obj({
        "policy": STR,
        "commands": _list(_obj({
            "time": _t("str", format="iso_time"), "command": STR,
            "kind": _t("str", enum=["session", "check", "collect", "lookup"]),
            "status": _t("str", enum=["sent", "ok", "refused", "timeout",
                                      "cancelled", "error"]),
        })),
        "config_check": _obj({
            "status": _t("str", enum=["unchanged", "changed", "unavailable",
                                      "unsupported"]),
            "command": STR, "before": OPT_STR, "after": OPT_STR,
        }, optional=["command", "before", "after"]),
        "via_panorama": _obj({"host": STR, "target": OPT_STR}),
    }, optional=["via_panorama"]),
}, optional=["parse_error"])


def _kind(v):
    return ("null" if v is None else "bool" if isinstance(v, bool) else
            "int" if isinstance(v, int) else "float" if isinstance(v, float) else
            "str" if isinstance(v, str) else "list" if isinstance(v, list) else
            "dict" if isinstance(v, dict) else type(v).__name__)


def _format_ok(fmt, value) -> bool:
    try:
        if fmt == "mac":
            return bool(_MAC.fullmatch(value))
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
    raise ValueError(f"unknown format {fmt!r}")


def check_value(spec, value, path, problems, warnings):
    """Check one value against its spec, recursively, adding a message per
    difference. A spec is {"type": [...]} plus "enum", "format", "keys" and
    "optional" (an object with known keys), "values" (an object with free
    keys), "items" (a list) or "facets" (the facets object)."""
    kind = _kind(value)
    if kind not in spec["type"]:
        problems.append(f"{path}: is {kind}, expected {' or '.join(spec['type'])}")
        return
    if value is None:
        return
    if "enum" in spec and value not in spec["enum"]:
        problems.append(f"{path}: {value!r} is not one of {spec['enum']}")
    if "format" in spec and not _format_ok(spec["format"], value):
        problems.append(f"{path}: {value!r} is not a valid {spec['format']}")
    if kind == "dict" and "keys" in spec:
        for key in value:
            if key not in spec["keys"]:
                problems.append(f"{path}: unexpected key {key!r}")
        for key, sub in spec["keys"].items():
            if key in value:
                check_value(sub, value[key], f"{path}.{key}", problems, warnings)
            elif key not in spec.get("optional", ()):
                problems.append(f"{path}: missing key {key!r}")
    elif kind == "dict" and "values" in spec:
        for key, item in value.items():
            check_value(spec["values"], item, f"{path}[{key!r}]", problems, warnings)
    elif kind == "dict" and spec.get("facets"):
        for key, item in value.items():
            if key in FACETS:
                check_value(FACETS[key], item, f"{path}.{key}", problems, warnings)
            else:
                warnings.append(f"{path}.{key}: facet not known to the checker, not checked")
    elif kind == "list" and "items" in spec:
        for i, item in enumerate(value):
            check_value(spec["items"], item, f"{path}[{i}]", problems, warnings)


def check_record(record, name=None):
    """Return (problems, warnings) for one facts record."""
    problems, warnings = [], []
    check_value(FACTS_SCHEMA, record, "$", problems, warnings)
    if problems:
        return problems, warnings
    device = record["device"]
    if name is not None and device["name"] != name:
        problems.append(f"$.device.name: {device['name']!r} is not the file's name {name!r}")
    names = [i["name"] for i in record["interfaces"]]
    if len(set(names)) != len(names):
        problems.append("$.interfaces: interface names are not unique")
    try:
        plan = collect.plan(device["platform"] or "")
    except readonly.CommandRefused as e:
        problems.append(f"$.device.platform: no read-only plan ({e})")
        plan = None
    if plan is not None:
        planned = [c for kind, c in plan if kind == "collect"]
        raw = list(record["raw"])
        extra = [c for c in raw if c not in planned]
        if extra:
            problems.append(f"$.raw: output for commands not in the plan: {extra}")
        elif raw != [c for c in planned if c in raw]:
            problems.append("$.raw: commands not in plan order")
        empty = [c for c in raw if not record["raw"][c].strip()]
        if empty:
            warnings.append(f"$.raw: empty output for {empty}")
        missing = [c for c in planned if c not in raw]
        if missing:
            warnings.append(f"no output kept for {len(missing)} planned command(s) "
                            f"(not supported or failed on this device): {missing}")
        _check_audit(record["audit"], device["platform"], plan, problems, warnings)
    if "parse_error" in record:
        warnings.append(f"$.parse_error: the driver could not parse this device: "
                        f"{record['parse_error']}")
    if not record["interfaces"]:
        warnings.append("$.interfaces: empty")
    known = set(names)
    for path, values in (
            ("$.neighbors[].local_intf", [n["local_intf"] for n in record["neighbors"]]),
            ("$.arp[].intf", [a["intf"] for a in record["arp"]]),
            ("$.mac_table[].intf", [m["intf"] for m in record["mac_table"]]),
            ("$.interfaces[].parent", [i["parent"] for i in record["interfaces"]])):
        unknown = sorted({v for v in values if v is not None and v not in known})
        if unknown:
            warnings.append(f"{path}: {len(unknown)} name(s) not among the device's "
                            f"interfaces: {unknown[:5]}")
    return problems, warnings


def _check_audit(audit, platform, plan, problems, warnings):
    entries = audit["commands"]
    for i, e in enumerate(entries):
        if e["status"] in ("refused", "cancelled"):
            problems.append(f"$.audit.commands[{i}]: {e['command']!r} was {e['status']}")
    status = audit["config_check"]["status"]
    if status == "changed":
        problems.append("$.audit.config_check: configuration changed during collection")
    elif status == "unavailable":
        warnings.append("$.audit.config_check: config-change marker could not be read, "
                        "so a change can't be ruled out")
    if drivers.transport(platform) != "ssh-cli":
        return  # API and PowerShell lines carry targets and hashes; audit.py checks them
    lines = [(e["kind"], e["command"]) for e in entries]
    if ("session", "enable") in lines[1:]:
        problems.append("$.audit.commands: 'enable' sent after the first line")
    if ("session", "exit") in lines[:-1]:
        problems.append("$.audit.commands: 'exit' sent before the last line")
    sent = [line for line in lines if not (line[0] == "session" and line[1] in SESSION_WORDS)]
    expected = [tuple(step) for step in plan]
    if sent != expected:
        timed_out = any(e["status"] == "timeout" for e in entries)
        if timed_out and sent == expected[:len(sent)]:
            warnings.append(f"$.audit.commands: stopped after a timeout, {len(sent)} of "
                            f"{len(expected)} planned lines sent")
        else:
            problems.append("$.audit.commands: the lines sent are not exactly the plan")


def summary(record) -> str:
    """One line describing what a facts record holds."""
    d = record.get("device") or {}
    intfs = record.get("interfaces") or []
    up = sum(1 for i in intfs if i.get("oper_up"))
    with_ip = sum(1 for i in intfs if i.get("ipv4"))
    raw = record.get("raw") or {}
    try:
        planned = sum(1 for kind, _ in collect.plan(d.get("platform") or "") if kind == "collect")
    except readonly.CommandRefused:
        planned = "?"
    return (f"{d.get('model') or '?'} {d.get('os_version') or '?'}, "
            f"{len(intfs)} interfaces ({up} up, {with_ip} with IPv4), "
            f"{len(record.get('neighbors') or [])} neighbors, {len(record.get('arp') or [])} ARP, "
            f"{len(record.get('mac_table') or [])} MAC, {len(raw)}/{planned} commands kept, "
            f"facets {sorted(record.get('facets') or {})}")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        sys.exit("usage: check_facts.py <facts_dir> | <file.json> ...")
    if len(argv) == 1 and Path(argv[0]).is_dir():
        files = sorted(Path(argv[0]).glob("*.json"))
    else:
        files = [Path(a) for a in argv]
    if not files:
        sys.exit(f"check_facts: no facts files in {argv[0]}")
    failed = warned = 0
    for path in files:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            print(f"FAIL  {path.stem}: no facts file ({path})")
            failed += 1
            continue
        except ValueError as e:
            print(f"FAIL  {path.stem}: not valid JSON ({e})")
            failed += 1
            continue
        problems, warnings = check_record(record, name=path.stem)
        label = "FAIL" if problems else "warn" if warnings else "ok"
        print(f"{label:<5} {path.stem}: {summary(record) if isinstance(record, dict) else ''}")
        for p in problems:
            print(f"        problem: {p}")
        for w in warnings:
            print(f"        warning: {w}")
        failed += bool(problems)
        warned += bool(warnings) and not problems
    print(f"CHECK {'FAIL' if failed else 'PASS'} {len(files)} file(s), "
          f"{failed} with problems, {warned} with warnings only")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
