# autotop-awx: rebuilding data collection by hand, with Copilot

Implementation guide · Written 2026-11-03 · Based on graham-grant/autotop-awx-cc `main` a3e6da7 · Replaces, for production, the stage 1 Copilot instructions and the exact-conformance method of 2026-10-31 and 2026-11-02

## What this guide is for

You'll write the collection part of autotop-awx yourselves, a few lines at a time. Copilot writes the small, self-contained functions, which are more than half of the code. The goal is a working, simple collector, not a copy of the original: it only has to do the same core jobs.

At the end of phase 1 you'll have an AWX job that:

1. logs in to each Cisco IOS / IOS-XE device over SSH;
2. runs only the whitelisted, read-only `show` commands its driver lists;
3. turns the output into one JSON file per device in the common multi-vendor format (schema 2);
4. checks every file against that format and the read-only rules, and fails the job if anything is wrong.

Nothing else (links, diagrams, Visio, feature layers) is built in phase 1.

**How to read this guide.** Section 1 lists the key functions of every stage of the project as goals. Section 2 sets the phase 1 milestones. Section 3 describes each part of the design, with a code sketch for the parts you write by hand. Section 4 is the build order and who writes what. Section 5 is how to maintain and extend the drivers. Section 6 has the Copilot prompts.

**What I tested.** The design in section 3 was run end to end on Python 3.9: the hand-written sketches with the Copilot functions filled in from the original parsers, against a test SSH server set up to answer like a Cisco device. It logged in with a password through the askpass helper, ran each command as its own exec request, refused non-whitelisted commands before anything was sent, reported a wrong password with the password masked, stopped a hung command at its timeout, and wrote a JSON file that the checker passed. It has not been run against a real Cisco device: that's the canary step in section 4.

---

## 1. The project's key functions, stage by stage

These are the jobs the original project does, reduced to what matters. Each is a goal to reach, in any reasonable way.

| Stage | Goal | Key functions |
| --- | --- | --- |
| **1. Collection** (this guide) | One JSON file per device, in one vendor-neutral format, gathered read-only | Read the inventory (AWX does this). For each device, pick its driver. Check every command against the whitelist before connecting. Log in and run each command. Read the configuration-change marker before and after. Parse the output into the common format, keeping the raw output. Record every command sent. Check the file. |
| 2. More vendors | Palo Alto firewalls and Windows servers in the same format | A PAN-OS driver over the XML API, with its own whitelist (only `<show>` operations and config `get`/`show`). A Windows driver that runs fixed, checked PowerShell scripts over OpenSSH. Both fill the same JSON fields. |
| 3. Links | Who is connected to whom | A link where LLDP or CDP says so (the exact cable). A link where a device's MAC is learned on a switch access port. A link where two devices share a subnet; a subnet with three or more devices becomes a "segment" node. Port-channels drawn as one link. |
| 4. Diagram | A Visio (and draw.io) file | Place each device (positions kept in a Visio template file people can edit). Route each link around the boxes. Label each end with the interface name and status colour. Write the file. |
| 5. Feature layers | Extra information on its own layer | IPsec tunnels, VLANs per port, firewall zones, server roles: each read from a "facet" of the JSON (`facets.ipsec`, `facets.vlans` ...) and drawn on a layer that can be switched on and off. |

Every later stage reads only the JSON files from stage 1. That's why the JSON format is the priority: if it's right, everything else can be built on it later, one piece at a time.

---

## 2. Phase 1 milestones

Do them in order. Each ends with something you can see working.

| # | Milestone | Done when |
| --- | --- | --- |
| 1.1 | The whitelist (`readonly.py`) and its tests | The tests in section 3.3 pass: listed `show` commands allowed, everything else refused. |
| 1.2 | The SSH transport (`sshexec.py`, `askpass.sh`) | From the AWX execution environment, a small script prints `show version` from one canary device. |
| 1.3 | Raw collection | `collect.py` writes a JSON file holding `device.name`, `raw` (each command's output) and `audit`. No parsing yet: `driver.parse()` returns `{}`. |
| 1.4 | The device and interfaces | `show version`, `show interfaces` and `show ip interface` parsed into `device` and `interfaces`. |
| 1.5 | Neighbors, ARP and MAC table | LLDP, CDP, ARP and MAC-table parsers fill `neighbors`, `arp` and `mac_table`. |
| 1.6 | Checking | `check_facts.py` passes on every file; the AWX job fails when it doesn't. |
| 1.7 | Roll-out | One canary device, then a few of each model and IOS release, then all of them. |

Milestone 1.3 is worth reaching early. Once raw output is being saved, you can develop and fix every parser offline from the saved files with `collect.py --reparse`, without logging in to a device again (section 5.4).

---

## 3. The design, and how to build it

### 3.1 Files and flow

```text
inventory.yml
playbooks/collect.yml          AWX job: check, collect, check the JSON
scripts/
  readonly.py                  the whitelist                      (by hand)
  sshexec.py, askpass.sh       runs one command over SSH          (by hand)
  record.py                    the empty JSON record              (by hand)
  collect.py                   one device -> one JSON file        (by hand)
  check_facts.py               checks JSON files                  (by hand + Copilot C12)
  drivers/
    __init__.py                platform name -> driver            (by hand)
    common.py                  MAC and interface-name helpers     (Copilot C1, C2)
    ios.py                     Cisco: commands and parsers        (Copilot C3-C11, glue by hand)
tests/                         one test file per module           (Copilot writes most)
```

```text
AWX job, per device:
  collect.py --name sw1 --host 10.0.0.1 --platform ios --out output/facts/sw1.json
    1. drivers.get_driver("ios")              -> drivers/ios.py
    2. every command in ios.COMMANDS          -> readonly.check()   (all of them, before connecting)
    3. config marker before                   -> sshexec.run_command()
    4. each command                           -> sshexec.run_command() -> raw[command] = output
    5. config marker after
    6. ios.parse(raw)                         -> device, interfaces, neighbors, arp, mac_table
    7. write the JSON (raw and audit included)
then once:
  check_facts.py output/facts                 -> fails the job on any problem
```

Rules kept from the original project, because they keep production safe and simple:

- Standard library only: no pip packages, no Ansible collections. It must run on Python 3.9.
- Credentials only from environment variables (AWX's "Network" credential sets them). Never on a command line, in a file or in a log.
- Only `sshexec.run_command()` contacts a device, and it checks the whitelist first.
- Only the drivers know vendor output. Everything else works on the JSON format.

### 3.2 The JSON record (schema 2)

This is the most important part of phase 1. Every driver, for every vendor, fills exactly this shape. Later stages read nothing else.

```json
{"schema": 2,
 "device": {"name": "sw1", "hostname": "sw1", "role": "switch", "vendor": "cisco",
            "platform": "ios", "model": "C9300-24T", "os_version": "17.09.04a",
            "serial": "FOC2401X0AB", "collected_at": "2026-11-03T08:56:46+00:00",
            "collector": "ssh-exec"},
 "interfaces": [{"name": "GigabitEthernet1/0/23", "kind": "physical",
                 "mac": "00:a1:b2:c3:01:23", "admin_up": true, "oper_up": true,
                 "speed_mbps": 1000, "mtu": 1500, "description": "", "vrf": "default",
                 "vlan": null, "parent": "Port-channel1", "ipv4": [], "ipv6": []}],
 "neighbors": [{"local_intf": "GigabitEthernet1/0/23", "protocol": "lldp",
                "remote_name": "sw2.lab.local", "remote_intf": "Gi1/0/23",
                "remote_mac": "00:a1:b2:c4:01:00", "remote_ip": "10.0.10.3",
                "remote_platform": null, "remote_capabilities": ["bridge", "router"]}],
 "arp": [{"ip": "10.0.10.11", "mac": "00:50:56:01:00:11",
          "intf": "GigabitEthernet1", "vrf": "default"}],
 "mac_table": [{"mac": "00:50:56:01:00:11", "intf": "GigabitEthernet1/0/5", "vlan": 10}],
 "facets": {},
 "raw": {"show version": "Cisco IOS XE Software, ...", "show interfaces": "..."},
 "audit": {"commands": [{"time": "2026-11-03T08:56:46+00:00", "command": "show version",
                         "status": "ok", "seconds": 0.3}],
           "config_check": {"before": "! Last configuration change at ...",
                            "after": "! Last configuration change at ...",
                            "status": "unchanged"}}}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `schema` | int | Always `2`. Raise it only if you change the format in a way that breaks old files. |
| `device.name` | string | The inventory name. Also the file name (`sw1.json`). |
| `device.hostname` | string | The device's own hostname, or the inventory name if it can't be read. |
| `device.role` | string or null | `router`, `switch`, `firewall` or `server`. |
| `device.vendor` | string or null | `cisco` (later `paloalto`, `microsoft`). |
| `device.platform` | string | The driver: `ios`. |
| `device.model`, `os_version`, `serial` | string or null | From `show version`. |
| `device.collected_at` | string | When collection started, ISO 8601 UTC. |
| `device.collector` | string | How it was collected: `ssh-exec`. |
| `interfaces[]` | list | One per interface. |
| `.name` | string | Always the full name (`GigabitEthernet1/0/1`, never `Gi1/0/1`), so names match across devices. |
| `.kind` | string | `physical`, `subinterface`, `lag`, `vlan`, `loopback`, `tunnel` or `virtual`. |
| `.mac` | string or null | Always `aa:bb:cc:dd:ee:ff`, lower case. |
| `.admin_up`, `.oper_up` | true, false or null | Administratively enabled; line protocol up. |
| `.speed_mbps` | number or null | Bandwidth in Mbit/s. |
| `.mtu` | int or null | |
| `.description` | string | `""` if none. |
| `.vrf` | string | `default` for the global table. |
| `.vlan` | int or null | The 802.1Q tag of a subinterface, or the VLAN of an SVI. |
| `.parent` | string or null | A subinterface's parent, or the port-channel a physical port belongs to. |
| `.ipv4`, `.ipv6` | list of strings | `10.0.0.1/30` form, primary address first. |
| `neighbors[]` | list | One per LLDP or CDP neighbor: `local_intf` (full name), `protocol` (`lldp` or `cdp`), `remote_name`, `remote_intf`, `remote_mac` (set when a server sends its MAC as the port id), `remote_ip`, `remote_platform`, `remote_capabilities` (`router`, `bridge`, `phone`, `wlan`, `station` ...). |
| `arp[]` | list | `ip`, `mac`, `intf` (full name), `vrf`. |
| `mac_table[]` | list | Learned dynamic entries: `mac`, `intf` (full name), `vlan`. CPU and router entries are left out. |
| `facets` | object | Optional extra data, one key per feature, e.g. `facets.vlans` (section 5.3). `{}` in phase 1. |
| `raw` | object | Each command's output, exactly as received. Kept so parsers can be fixed and re-run later. |
| `audit` | object | Every command sent, and the configuration check. |

Rules every driver follows:

- Every key is always present, in the order above. A value that isn't known is `null` (or `""`, `[]` where the table says so), never missing.
- Interface names are always expanded to the device's own full names (`canonical_intf`, prompt C2).
- MACs are always normalised (`norm_mac`, prompt C1).
- A command a device doesn't support is skipped and its part stays empty; it isn't an error.

### 3.3 The whitelist (`scripts/readonly.py`)

The guarantee: nothing reaches a device unless it is on the driver's list **and** has a read-only shape. Two independent checks, so a mistake in one is caught by the other:

1. **Exact list, deny by default.** The command must be one of the strings the driver lists in `COMMANDS` (or its config-check command). Anything else is refused, however harmless it looks.
2. **Shape.** The command must also match the platform's read-only pattern. For IOS that's `show` followed by plain words, optionally with one `| include`, `| exclude`, `| begin` or `| section` filter. That stops someone adding `clear counters` or `show run | redirect flash:x` to a driver's list by mistake. A platform with no pattern can't be sent anything.

The pattern also rules out newlines, `;`, `?`, tabs, doubled or trailing spaces and abbreviations like `sh`, so nothing can be smuggled onto the line.

Write this by hand. It is short on purpose:

```python
"""Command whitelist: the only gate between the collector and a device."""
import re


class CommandRefused(Exception):
    pass


# The shape every command for a platform must have. Deny by default: a
# platform not listed here can't be sent anything.
SHAPES = {
    "ios": re.compile(
        r"show( [A-Za-z0-9._/:-]+)+"
        r"( \| (include|exclude|begin|section) [A-Za-z0-9._/:-]+( [A-Za-z0-9._/:-]+)*)?"),
}


def check(platform, command, allowed):
    """Return command if it may be sent to a device of this platform,
    else raise CommandRefused. `allowed` is the driver's command list."""
    shape = SHAPES.get(platform)
    if shape is None:
        raise CommandRefused(f"{platform!r}: no whitelist for this platform")
    if not isinstance(command, str) or command not in allowed:
        raise CommandRefused(f"{platform}: {command!r} is not in the driver's command list")
    if not shape.fullmatch(command):
        raise CommandRefused(f"{platform}: {command!r} is not a read-only command shape")
    return command
```

Tests to write (Copilot can write them from this list; check every case by eye, since this is the safety layer):

- allowed, when in the list: `show version`, `show interfaces GigabitEthernet1/0/1`, `show running-config | include configuration change`;
- refused even when in the list: `clear counters`, `configure terminal`, `conf t`, `write memory`, `reload`, `copy running-config startup-config`, `show run | redirect flash:x`, `show version | tee x`, `show run | include a;b`, `show version\nreload`, `show version ` (trailing space), `show  version`, `sh version`, `show version?`, `show version | include` (no text), `""`, `None`;
- refused when not in the list: `show clock`;
- refused for a platform with no pattern: `junos`.

### 3.4 The SSH transport (`scripts/sshexec.py`, `scripts/askpass.sh`)

**The simplification.** The original project drove an interactive SSH session through a pseudo-terminal: it typed each command, watched for prompts, answered pagers and cancelled confirmation questions. That was the hardest code in the project. Here each command is instead its own SSH **exec request**: `ssh -l user host "show version"`, the same as typing that at a Linux prompt. Cisco IOS runs the one command, prints its output and closes the connection.

Why this is simpler and also safer:

- There is no terminal and nothing is typed. Standard input is closed (`stdin=DEVNULL`), so even a command that asked "Proceed? [confirm]" could never be answered.
- IOS doesn't page output on an exec request, so there's no `--More--` handling. (The checker flags any `--More--` in case a device does.)
- No prompt parsing, no `enable`, no `terminal length 0`.

The password is passed the standard OpenSSH way for scripts: `ssh` runs the program named in `SSH_ASKPASS` and reads the password from it. `askpass.sh` prints the password from an environment variable that only that `ssh` process has. It is never on a command line or in a file. This needs OpenSSH 8.4 or later on the AWX execution environment (for `SSH_ASKPASS_REQUIRE=force`); check with `ssh -V`.

The costs: one SSH login per command (about ten per device), and every login appears in the device's AAA log. The collection account must land at a privilege level that can run the commands without `enable` (privilege 15, or a custom level allowing these `show` commands). If a device limits concurrent VTY sessions, lower the AWX job's forks.

`askpass.sh` (make it executable: `chmod 755`, and `git update-index --chmod=+x scripts/askpass.sh`):

```sh
#!/bin/sh
# ssh runs this to ask for the password; it prints the one collect.py
# put in the environment. The password never appears on a command line.
printf '%s\n' "$AUTOTOP_SSH_PASSWORD"
```

`sshexec.py`, by hand:

```python
"""Runs one command on a device over SSH, as a single exec request: no
shell, no terminal, nothing typed. The only place that contacts a device."""
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import readonly

ASKPASS = str(Path(__file__).resolve().parent / "askpass.sh")


class SshError(Exception):
    pass


def run_command(host, username, password, platform, command, allowed,
                port=22, timeout=60, ssh_args=(), audit=None):
    """Return the output of one whitelisted command. Every attempt is
    recorded in `audit` (a list), refused ones too."""
    entry = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "command": command, "status": "refused"}
    if audit is not None:
        audit.append(entry)
    readonly.check(platform, command, allowed)          # raises before anything is sent
    argv = ["ssh", "-T",
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR", "-o", "PubkeyAuthentication=no",
            "-o", "PreferredAuthentications=keyboard-interactive,password",
            "-o", "NumberOfPasswordPrompts=1", "-o", f"ConnectTimeout={int(timeout)}",
            "-p", str(port), *ssh_args, "-l", username, host, command]
    env = dict(os.environ, SSH_ASKPASS=ASKPASS, SSH_ASKPASS_REQUIRE="force",
               DISPLAY=os.environ.get("DISPLAY") or "none", AUTOTOP_SSH_PASSWORD=password)
    entry["status"] = "sent"
    started = time.monotonic()
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env, timeout=timeout,
                              start_new_session=True)
    except subprocess.TimeoutExpired:
        entry["status"] = "timeout"
        raise SshError(f"{host}: {command!r} timed out after {timeout}s")
    entry["seconds"] = round(time.monotonic() - started, 1)
    output = proc.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
    if proc.returncode == 255:      # ssh's own failure: connection or login
        entry["status"] = "error"
        msg = proc.stderr.decode("utf-8", "replace").strip()
        if password:
            msg = msg.replace(password, "****")
        raise SshError(f"{host}: ssh failed: {msg}")
    entry["status"] = "ok"
    return output
```

Return code 255 is `ssh` itself failing (no connection, login refused). Any other code is the device's, and the output is kept; `collect.py` decides whether it's an error message.

### 3.5 The empty record (`scripts/record.py`) and the driver registry (`scripts/drivers/__init__.py`)

```python
"""The device record (schema 2): the one JSON shape every driver fills."""
SCHEMA = 2


def empty_record(name, platform):
    return {
        "schema": SCHEMA,
        "device": {"name": name, "hostname": name, "role": None, "vendor": None,
                   "platform": platform, "model": None, "os_version": None,
                   "serial": None, "collected_at": None, "collector": "ssh-exec"},
        "interfaces": [], "neighbors": [], "arp": [], "mac_table": [],
        "facets": {}, "raw": {}, "audit": {},
    }
```

```python
"""Driver registry: platform name -> driver module."""
from drivers import ios

DRIVERS = {"ios": ios}
ALIASES = {"iosxe": "ios", "ios-xe": "ios", "cisco.ios.ios": "ios"}


def get_driver(platform):
    key = (platform or "").lower()
    return DRIVERS.get(ALIASES.get(key, key))
```

### 3.6 The Cisco driver (`scripts/drivers/ios.py`)

A driver is one module with:

| Name | What it is |
| --- | --- |
| `PLATFORM` | `"ios"`: the whitelist pattern it uses and `device.platform`. |
| `VENDOR` | `"cisco"`. |
| `COMMANDS` | The exact commands to run, in order. The whitelist allows only these. |
| `CONFIG_CHECK_COMMAND` | A read-only command whose output changes when the configuration does: `show running-config \| include configuration change` (the `! Last configuration change at ...` line). |
| `looks_like_error(output)` | True if the device rejected a command (prompt C4). |
| `parse(raw)` | Takes `{command: output}` and returns the record's parts: `device`, `interfaces`, `neighbors`, `arp`, `mac_table`, `facets`. |
| `parse_...(text)` functions | One per command, each turning one command's text into Python data (prompts C3, C5 to C11). |

The top of the module, by hand:

```python
"""Cisco IOS / IOS-XE driver: commands and parsers."""
import re

from drivers.common import canonical_intf, norm_mac

PLATFORM = "ios"
VENDOR = "cisco"
COMMANDS = ["show version", "show interfaces", "show ip interface",
            "show lldp neighbors detail", "show cdp neighbors detail",
            "show ip arp", "show mac address-table dynamic"]
CONFIG_CHECK_COMMAND = "show running-config | include configuration change"
```

Then the Copilot functions (C3 to C11), then `parse()`, which joins them up. Write `parse()` by hand; it's where the driver's knowledge of how the commands relate lives:

```python
def parse(raw):
    """All of this device's raw output -> the record's parts."""
    version = parse_show_version(raw.get("show version", ""))
    interfaces = parse_show_interfaces(raw.get("show interfaces", ""))
    for name, ip in parse_show_ip_interface(raw.get("show ip interface", "")).items():
        if name not in interfaces:          # only in "show ip interface"
            kind, parent = kind_and_parent(name)
            interfaces[name] = {"name": name, "kind": kind, "mac": None, "admin_up": None,
                                "oper_up": None, "speed_mbps": None, "mtu": None,
                                "description": "", "vrf": "default", "vlan": None,
                                "parent": parent, "ipv4": [], "ipv6": []}
        interfaces[name]["vrf"] = ip["vrf"]
        interfaces[name]["ipv4"] = ip["ipv4"]
    names = list(interfaces)
    neighbors = (parse_lldp_neighbors(raw.get("show lldp neighbors detail", ""))
                 + parse_cdp_neighbors(raw.get("show cdp neighbors detail", "")))
    for n in neighbors:
        n["local_intf"] = canonical_intf(n["local_intf"], names)
    arp = parse_arp(raw.get("show ip arp", ""))
    for a in arp:
        a["intf"] = canonical_intf(a["intf"], names)
        a["vrf"] = interfaces.get(a["intf"], {}).get("vrf", "default")
    mac_table = parse_mac_table(raw.get("show mac address-table dynamic", ""))
    for m in mac_table:
        m["intf"] = canonical_intf(m["intf"], names)
    return {
        "device": {"hostname": version["hostname"], "role": version["role"],
                   "model": version["model"], "os_version": version["os_version"],
                   "serial": version["serial"]},
        "interfaces": list(interfaces.values()),
        "neighbors": neighbors, "arp": arp, "mac_table": mac_table, "facets": {},
    }
```

For milestone 1.3, start with `def parse(raw): return {}`.

### 3.7 The collector (`scripts/collect.py`)

By hand. It joins the parts and has two modes: collect from a device, or `--reparse` a saved file without contacting anything.

```python
#!/usr/bin/env python3
"""Collect one device: run its driver's whitelisted commands over SSH and
write one schema 2 JSON file."""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

import drivers
import readonly
import record as rec
import sshexec


def collect(name, host, platform, port=22, timeout=60):
    driver = drivers.get_driver(platform)
    if driver is None:
        raise SystemExit(f"{name}: no driver for platform {platform!r}")
    allowed = list(driver.COMMANDS) + [driver.CONFIG_CHECK_COMMAND]
    for command in allowed:                    # everything checked before connecting
        readonly.check(driver.PLATFORM, command, allowed)
    username = os.environ.get("NET_USERNAME") or os.environ.get("ANSIBLE_NET_USERNAME")
    password = os.environ.get("NET_PASSWORD") or os.environ.get("ANSIBLE_NET_PASSWORD")
    if not username or not password:
        raise SystemExit(f"{name}: no credentials (NET_USERNAME / NET_PASSWORD)")
    record = rec.empty_record(name, driver.PLATFORM)
    record["device"]["collected_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    audit = []

    def run(command):
        return sshexec.run_command(host, username, password, driver.PLATFORM, command,
                                   allowed, port=port, timeout=timeout, audit=audit)

    before = run(driver.CONFIG_CHECK_COMMAND).strip()
    for command in driver.COMMANDS:
        try:
            output = run(command)
        except sshexec.SshError as e:
            print(f"{name}: {e}", file=sys.stderr)
            continue
        if driver.looks_like_error(output):
            print(f"{name}: {command!r} not supported, skipped", file=sys.stderr)
            continue
        record["raw"][command] = output
    after = run(driver.CONFIG_CHECK_COMMAND).strip()
    fill(record, driver)
    if driver.looks_like_error(before) or driver.looks_like_error(after):
        status = "unavailable"             # the account can't read the marker
    else:
        status = "unchanged" if before == after else "changed"
    record["audit"] = {"commands": audit,
                       "config_check": {"before": before, "after": after, "status": status}}
    return record


def fill(record, driver):
    parsed = driver.parse(record["raw"])
    for key, value in parsed.pop("device", {}).items():
        if value is not None:
            record["device"][key] = value
    record["device"]["vendor"] = driver.VENDOR
    for key in ("interfaces", "neighbors", "arp", "mac_table", "facets"):
        record[key] = parsed.get(key, record[key])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name")
    ap.add_argument("--host")
    ap.add_argument("--platform")
    ap.add_argument("--out", required=True)
    ap.add_argument("--port", type=int, default=22)
    ap.add_argument("--reparse", help="re-run the parsers on this facts file's raw output; no device is contacted")
    a = ap.parse_args()
    if a.reparse:
        with open(a.reparse, encoding="utf-8") as f:
            record = json.load(f)
        driver = drivers.get_driver(record["device"]["platform"])
        fill(record, driver)
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
        print(f"{record['device']['name']}: reparsed")
        return
    record = collect(a.name, a.host, a.platform, a.port)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    print(f"{a.name}: {len(record['raw'])} command(s), config {record['audit']['config_check']['status']}")
    if record["audit"]["config_check"]["status"] == "changed":
        sys.exit(3)


if __name__ == "__main__":
    main()
```

Exit codes: 0 when collected, 3 when the configuration-change marker moved during collection (someone else changed the device, since we only send `show` commands), and an error message for no driver or no credentials. A refused command raises `CommandRefused` before anything is sent. If the account isn't allowed to run `show running-config`, the marker comes back as an error and the check is recorded as `unavailable`; the checker then fails the file, so give the account that permission.

### 3.8 Checking the JSON (`scripts/check_facts.py`)

This is how you verify that what the devices returned conforms to the format. The specification is written as plain Python data, one dict per object, so it reads like the table in section 3.2: `str` means a string, `(str, N)` a string or null, `[INTERFACE]` a list of interface objects. `check_types()` (prompt C12) walks a record against it. The rest checks values and the read-only audit, and is written by hand.

```python
#!/usr/bin/env python3
"""Checks facts files against the record format and the audit rules.
Prints problems by JSON path, never values. Exit 1 on any problem."""
import ipaddress
import json
import re
import sys
from pathlib import Path

import drivers

N = None  # "may be null"
DEVICE = {"name": str, "hostname": str, "role": (str, N), "vendor": (str, N),
          "platform": str, "model": (str, N), "os_version": (str, N), "serial": (str, N),
          "collected_at": (str, N), "collector": str}
INTERFACE = {"name": str, "kind": str, "mac": (str, N), "admin_up": (bool, N),
             "oper_up": (bool, N), "speed_mbps": (int, float, N), "mtu": (int, N),
             "description": str, "vrf": str, "vlan": (int, N), "parent": (str, N),
             "ipv4": [str], "ipv6": [str]}
NEIGHBOR = {"local_intf": str, "protocol": str, "remote_name": (str, N),
            "remote_intf": (str, N), "remote_mac": (str, N), "remote_ip": (str, N),
            "remote_platform": (str, N), "remote_capabilities": [str]}
ARP = {"ip": str, "mac": (str, N), "intf": (str, N), "vrf": str}
MAC = {"mac": str, "intf": str, "vlan": (int, N)}
AUDIT_ENTRY = {"time": str, "command": str, "status": str, "seconds": (int, float, N)}
RECORD = {"schema": int, "device": DEVICE, "interfaces": [INTERFACE],
          "neighbors": [NEIGHBOR], "arp": [ARP], "mac_table": [MAC], "facets": dict,
          "raw": dict,
          "audit": {"commands": [AUDIT_ENTRY],
                    "config_check": {"before": str, "after": str, "status": str}}}
OPTIONAL = {"seconds"}   # keys that may be missing

KINDS = {"physical", "subinterface", "lag", "vlan", "loopback", "tunnel", "virtual"}
_MAC = re.compile(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}")


def check_types(value, spec, path, problems):
    ...   # Copilot prompt C12


def check_record(record):
    problems = []
    check_types(record, RECORD, "$", problems)
    if problems:
        return problems
    for i, intf in enumerate(record["interfaces"]):
        if intf["kind"] not in KINDS:
            problems.append(f"$.interfaces[{i}].kind: unknown kind")
        if intf["mac"] is not None and not _MAC.fullmatch(intf["mac"]):
            problems.append(f"$.interfaces[{i}].mac: not aa:bb:cc:dd:ee:ff")
        for j, a in enumerate(intf["ipv4"]):
            try:
                ipaddress.IPv4Interface(a)
                assert "/" in a
            except (ValueError, AssertionError):
                problems.append(f"$.interfaces[{i}].ipv4[{j}]: not address/prefix")
    driver = drivers.get_driver(record["device"]["platform"])
    allowed = set(driver.COMMANDS) | {driver.CONFIG_CHECK_COMMAND} if driver else set()
    for i, entry in enumerate(record["audit"]["commands"]):
        if entry["command"] not in allowed:
            problems.append(f"$.audit.commands[{i}]: command not in the driver's list")
        if entry["status"] == "refused":
            problems.append(f"$.audit.commands[{i}]: refused")
    status = record["audit"]["config_check"]["status"]
    if status == "changed":
        problems.append("$.audit.config_check: configuration changed during collection")
    elif status != "unchanged":
        problems.append("$.audit.config_check: the change marker could not be read")
    for command, output in record["raw"].items():
        if "--More--" in output:
            problems.append(f"$.raw: output of {command!r} was paged")
    return problems


def main():
    files = sorted(Path(sys.argv[1]).glob("*.json"))
    failed = 0
    for path in files:
        problems = check_record(json.loads(path.read_text(encoding="utf-8")))
        print(f"{'FAIL' if problems else 'ok  '}  {path.name}")
        for p in problems:
            print(f"      {p}")
        failed += bool(problems)
    print(f"checked {len(files)} file(s), {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
```

It prints `ok` or `FAIL` per file with the JSON path of each problem, and exits 1 if any file failed, which fails the AWX job. The checks:

- every key present, no extra keys, every value of the right type (`true` isn't accepted for a number);
- interface kinds from the allowed list, MACs in `aa:bb:cc:dd:ee:ff` form, IPv4 addresses as `address/prefix`;
- every command in the audit is on the driver's list, none was refused;
- the configuration didn't change during collection, and the change marker could be read;
- no output was paged.

### 3.9 The AWX job (`playbooks/collect.yml`)

```yaml
---
# Read-only collection: one JSON file per device in output/facts/.
- name: Prepare
  hosts: localhost
  connection: local
  gather_facts: no
  tasks:
    - name: Create the output directory
      ansible.builtin.file:
        path: "{{ playbook_dir }}/../output/facts"
        state: directory
        mode: "0755"

- name: Collect from each device
  hosts: network_devices
  connection: local          # collect.py does the SSH; Ansible never connects
  gather_facts: no
  tasks:
    - name: Run collect.py
      ansible.builtin.command: >-
        python3 {{ playbook_dir }}/../scripts/collect.py
        --name {{ inventory_hostname }}
        --host {{ ansible_host | default(inventory_hostname) }}
        --platform {{ ansible_network_os }}
        --out {{ playbook_dir }}/../output/facts/{{ inventory_hostname }}.json
      environment:           # from AWX's "Network" credential; never on the command line
        NET_USERNAME: "{{ lookup('env', 'ANSIBLE_NET_USERNAME') }}"
        NET_PASSWORD: "{{ lookup('env', 'ANSIBLE_NET_PASSWORD') }}"
      register: result
      changed_when: false

    - name: Show what was collected
      ansible.builtin.debug:
        var: result.stdout_lines

- name: Check every facts file
  hosts: localhost
  connection: local
  gather_facts: no
  tasks:
    - name: Run check_facts.py
      ansible.builtin.command: >-
        python3 {{ playbook_dir }}/../scripts/check_facts.py
        {{ playbook_dir }}/../output/facts
      register: check
      changed_when: false
      failed_when: false

    - name: Show the check
      ansible.builtin.debug:
        var: check.stdout_lines

    - name: Fail the job if a file doesn't conform
      ansible.builtin.assert:
        that: check.rc == 0
        fail_msg: "A facts file doesn't match the format - see the check above."
```

Attach a credential of the built-in "Network" type to the job template. Use the inventory's `ansible_network_os: ios` for each Cisco device. To see the files, add a play that copies `output/facts/` somewhere, or print one with `debug`, since the job's container is discarded afterwards.

---

## 4. Build order, and who writes what

| Step | Milestone | By hand | Copilot (section 6) | Check |
| --- | --- | --- | --- | --- |
| 1 | 1.1 | `readonly.py` | the tests in 3.3 | `python3 -m unittest discover -s tests` |
| 2 | 1.2 | `askpass.sh`, `sshexec.py` | | From the AWX environment: `python3 -c "import sshexec; print(sshexec.run_command(HOST, USER, PASS, 'ios', 'show version', ['show version']))"` with `scripts/` as the current directory, against one canary device |
| 3 | 1.3 | `record.py`, `drivers/__init__.py`, the top of `drivers/ios.py` with `parse()` returning `{}`, `collect.py`, `playbooks/collect.yml` | C4 `looks_like_error` | One canary run: the JSON has `raw` and `audit`; the device's own command log (TACACS+ or `show logging`) lists exactly the commands in the audit |
| 4 | 1.4 | `parse()` (first part) | C1 `norm_mac`, C2 `canonical_intf`, C3 `kind_and_parent`, C5 `parse_show_version`, C7 `parse_show_interfaces`, C6 `parse_show_ip_interface` | Unit tests; `collect.py --reparse` on the canary's file fills `device` and `interfaces` |
| 5 | 1.5 | the rest of `parse()` | C8 `parse_arp`, C9 `parse_mac_table`, C10 `parse_cdp_neighbors`, C11 `parse_lldp_neighbors` | Unit tests; reparse again |
| 6 | 1.6 | `check_facts.py` (spec and checks) | C12 `check_types`, and tests for it | `check_facts.py output/facts` passes; edit a value in a copy to see it fail |
| 7 | 1.7 | | | Canary, then a few per model and IOS release, then all |

By line count, the Copilot functions and their tests are well over half of the final code. The hand-written parts are the ones that decide safety (whitelist, transport, collector) and the ones that join things up.

**Working with Copilot on each prompt:**

1. Paste the prompt as it stands. Each one is self-contained: what the function takes, what it returns, the rules, and worked examples.
2. Read the function before using it. It should be short. Reject anything that imports a non-standard module, prints, or catches exceptions broadly to hide problems.
3. Run the tests Copilot writes with it. The expected values in the prompts came from running the original project's parsers, so they're known to be right.
4. If a test fails, paste the failure back with "Fix the function so this test passes; don't change the test."
5. Then try it on your own devices' saved output with `--reparse` (section 5.4). Real output is the real test.

**Going live safely:**

- Before the first device, give the collection account device-side limits if you can: TACACS+ command authorisation that permits only `show` commands, with command accounting on. Then even a bug in our code can't change a device.
- Run the canary first and compare the device's own command log with the JSON's `audit.commands`: they should list the same commands and nothing else.
- Check that `audit.config_check.status` is `unchanged` on every file.

---

## 5. Maintaining and extending the drivers

### 5.1 The idea

Vendor knowledge lives only in `scripts/drivers/`. A driver turns one platform's output into the common JSON format; nothing outside the drivers ever reads raw output. That's what lets a firewall, a switch and a server sit in the same diagram later: the diagram reads `interfaces`, `neighbors` and the rest, never `show` output.

Three rules keep it maintainable:

1. **One command, one parser.** Each command in `COMMANDS` has one `parse_...(text)` function that only turns that text into Python data. It doesn't know about other commands.
2. **`parse()` joins them.** Anything that combines commands (an IP address from `show ip interface` added to an interface from `show interfaces`, abbreviated names expanded) happens in `parse()`.
3. **Raw output is always kept.** A parser can be fixed and re-run on files you already have.

### 5.2 Adding a field from a command the driver already runs

Example: interface duplex from `show interfaces`.

1. Add the field to the format (section 3.2 and the `INTERFACE` spec in `check_facts.py`), with its type and what "unknown" is (`null`).
2. Change the parser (`parse_show_interfaces`) to fill it. With Copilot: paste the original prompt C7, plus "also add the key `duplex`: `full`, `half` or `None`, from `Full-duplex` / `Half-duplex` in the block", plus one example from your own saved output.
3. Add the field to the interface dict that `parse()` makes for interfaces only in `show ip interface`.
4. Run `collect.py --reparse` on saved files and `check_facts.py` on the results. No device needed.

Every driver must then fill the field (with `null` if its platform can't supply it), because `check_facts.py` requires every key.

### 5.3 Adding a command (a new facet)

Example: VLANs, the way the original project did it. VLAN data isn't something every device has, so it goes in a **facet**, `facets.vlans`, not in the core.

1. **Design the facet's shape first**, vendor-neutral, and write it down next to section 3.2:
   ```json
   "facets": {"vlans": {"vlans":  [{"id": 10, "name": "SERVERS", "ports": ["GigabitEthernet1/0/1"]}],
                        "trunks": [{"port": "Port-channel1", "native": 1,
                                    "allowed": "1-4094", "active": "1,10,20"}]}}
   ```
2. **Check the commands are read-only** and add them to the driver's `COMMANDS`: `show vlan brief`, `show interfaces trunk`. The whitelist allows them automatically once they're listed, as long as they match the `show` shape. If a command doesn't match the shape, don't widen the pattern: find a different, read-only command.
3. **Write one parser per command** with Copilot: prompts C13 and C14 in section 6.
4. **Join them in `parse()`**, expanding port names to full names:
   ```python
   if raw.get("show vlan brief") is not None:
       vlans = parse_vlan_brief(raw["show vlan brief"])
       for v in vlans:
           v["ports"] = [canonical_intf(p, names) for p in v["ports"]]
       trunks = parse_interfaces_trunk(raw.get("show interfaces trunk", ""))
       for t in trunks:
           t["port"] = canonical_intf(t["port"], names)
       facets["vlans"] = {"vlans": vlans, "trunks": trunks}
   ```
   and return `facets` instead of `{}`.
5. **Add the facet's shape** to `check_facts.py`: replace `"facets": dict` in `RECORD` with `{"vlans": {...}}`, and add `"vlans"` to `OPTIONAL` so devices without VLANs (routers) still pass.
6. **Collect again** (the new commands need a run), then check.

A device that doesn't support a command (a router has no `show vlan brief`) returns an error; `collect.py` skips it, and the facet is simply missing.

### 5.4 Fixing a parser without touching a device

Because raw output is kept, parser work happens offline:

```bash
cd scripts
python3 collect.py --reparse ../output/facts/sw1.json --out /tmp/sw1.json
python3 check_facts.py /tmp            # or look at /tmp/sw1.json
```

When a device's output isn't parsed right: copy that command's text out of the file's `raw` section, add it as a new test case for the parser (input and the result you expect), fix the parser until the test passes, then reparse every saved file and check them. Copilot is good at this: paste the original prompt, the failing input and the expected result.

### 5.5 Adding a vendor

Example: Arista EOS or Cisco NX-OS, which also answer SSH exec requests.

1. **Whitelist first.** Add the platform's read-only pattern to `SHAPES` in `readonly.py`, and tests for it, before anything else. Until then the platform can't be contacted at all. Keep the pattern as narrow as the vendor allows (only its `show` verb, plain words, display filters that can't write files).
2. **Write `scripts/drivers/<platform>.py`** with the same names: `PLATFORM`, `VENDOR`, `COMMANDS`, `CONFIG_CHECK_COMMAND` (or `None` if the platform has no such marker; then make `collect.py` skip the before/after check and record the status as `unsupported`, and let `check_facts.py` accept `unsupported` for that platform only), `looks_like_error`, the `parse_...` functions and `parse()`.
3. **Register it** in `drivers/__init__.py` (`DRIVERS`, and any `ALIASES` for the `ansible_network_os` names your inventory uses).
4. **Fill the same format.** Map the vendor's terms onto the fields in section 3.2: its interface types onto `kind`, its routing instance onto `vrf`, its MAC format through `norm_mac`. A field the vendor can't supply is `null`, never left out.
5. **Get sample output first.** Run the commands by hand on a lab device, save the output, and write and test the parsers from it before the first collection run.
6. If the vendor needs an interface kind that isn't in the list, add it to `KINDS` in `check_facts.py` and to the table in section 3.2, so every driver and every later stage knows it.

Palo Alto (XML API over HTTPS) and Windows (PowerShell) don't answer `show` commands over SSH. They need their own transport and their own whitelist, as in the original project's design notes (`docs/design-archive/2026-10-26/phase-3-palo-alto.md` and `phase-4-windows.md`). Plan those as a separate piece of work.

### 5.6 What never changes

- No command reaches a device except through `sshexec.run_command()`, which checks the whitelist.
- A refused command is never "fixed" by widening the pattern. Use a different read-only command.
- Nothing ever answers a device's question. `stdin` stays closed.
- Credentials stay in environment variables.

---

## 6. Copilot prompts

Each prompt is complete on its own: paste it as it is. The expected results were produced by running the original project's parsers on the same input, so a function that returns them is right. The sample outputs are from a lab, not production.

Write the functions into the files named, under the hand-written top of each file. `drivers/ios.py` needs `import re` and `from drivers.common import canonical_intf, norm_mac` at the top for C7 to C11.

### Phase 1

#### Prompt C1: `norm_mac(text)`

Goes in `scripts/drivers/common.py`.

```text
Write one Python function for a read-only network data collector.
Use only the Python standard library. It must run on Python 3.9.
Do not print, log, or raise for unexpected input: return the empty
result described below instead.

def norm_mac(text):
Turn a MAC address written in any common style ("5254.0012.3456",
"52-54-00-12-34-56", "52:54:00:12:34:56") into lower-case colon form
"52:54:00:12:34:56". Return None if text is None or empty, if it does not
contain exactly 12 hex digits, or if it contains any character other than
hex digits, ".", ":", "-" and whitespace.

Example 1 call: norm_mac('5254.0012.3456')
Example 1 must return:
'52:54:00:12:34:56'

Example 2 call: norm_mac('52-54-00-12-34-56')
Example 2 must return:
'52:54:00:12:34:56'

Example 3 call: norm_mac('52:54:00:AB:CD:EF')
Example 3 must return:
'52:54:00:ab:cd:ef'

Example 4 call: norm_mac('5254.0012.345')
Example 4 must return:
None

Example 5 call: norm_mac('zz54.0012.3456')
Example 5 must return:
None

Example 6 call: norm_mac('')
Example 6 must return:
None

Example 7 call: norm_mac(None)
Example 7 must return:
None

Also write a unittest.TestCase with one test per example above,
asserting the return value is exactly equal (assertEqual).
```

#### Prompt C2: `canonical_intf(name, known)`

Goes in `scripts/drivers/common.py`.

```text
Write one Python function for a read-only network data collector.
Use only the Python standard library. It must run on Python 3.9.
Do not print, log, or raise for unexpected input: return the empty
result described below instead.

def canonical_intf(name, known):
Cisco output often abbreviates interface names ("Gi1/0/1" for
"GigabitEthernet1/0/1"). Given a name and the list of the device's own
full interface names (known), return the full name it refers to.
- If name is None or empty, return it unchanged.
- An exact match wins; then a case-insensitive exact match.
- Otherwise split name into a letter prefix and the rest, which starts at
  the first digit (use the regular expression ^([A-Za-z][A-Za-z-]*?)\s*(\d\S*)$).
  A known name matches if it splits the same way, has the same rest, and
  its prefix (lower-case) starts with name's prefix (lower-case).
- Return the match only if exactly one known name matches; otherwise
  return name unchanged.
In the examples, known is:
['GigabitEthernet1/0/1',
 'GigabitEthernet1/0/2',
 'Port-channel1',
 'TenGigabitEthernet1/1/1',
 'Vlan10']

Example 1 call: canonical_intf('Gi1/0/1', known)
Example 1 must return:
'GigabitEthernet1/0/1'

Example 2 call: canonical_intf('gigabitethernet1/0/2', known)
Example 2 must return:
'GigabitEthernet1/0/2'

Example 3 call: canonical_intf('Po1', known)
Example 3 must return:
'Port-channel1'

Example 4 call: canonical_intf('Te1/1/1', known)
Example 4 must return:
'TenGigabitEthernet1/1/1'

Example 5 call: canonical_intf('Vl10', known)
Example 5 must return:
'Vlan10'

Example 6 call: canonical_intf('Fa0/1', known)
Example 6 must return:
'Fa0/1'

Example 7 call: canonical_intf('GigabitEthernet1/0/1', known)
Example 7 must return:
'GigabitEthernet1/0/1'

Example 8 call: canonical_intf(None, known)
Example 8 must return:
None

Also write a unittest.TestCase with one test per example above,
asserting the return value is exactly equal (assertEqual).
```

#### Prompt C3: `kind_and_parent(name)`

Goes in `scripts/drivers/ios.py`.

```text
Write one Python function for a read-only network data collector.
Use only the Python standard library. It must run on Python 3.9.
Do not print, log, or raise for unexpected input: return the empty
result described below instead.

def kind_and_parent(name):
Return a tuple (kind, parent) for a Cisco interface name.
- A name containing "." is a subinterface: ("subinterface", the part
  before the first ".").
- Otherwise the first matching prefix decides the kind, with parent None:
  "Loopback" -> "loopback", "Tunnel" -> "tunnel", "Port-channel" -> "lag",
  "Vlan" -> "vlan", "BDI" -> "vlan", "NVI" -> "virtual", "Null" -> "virtual",
  "Virtual" -> "virtual".
- Anything else is ("physical", None).

Example 1 call: kind_and_parent('GigabitEthernet1.20')
Example 1 must return:
('subinterface', 'GigabitEthernet1')

Example 2 call: kind_and_parent('Loopback0')
Example 2 must return:
('loopback', None)

Example 3 call: kind_and_parent('Tunnel1')
Example 3 must return:
('tunnel', None)

Example 4 call: kind_and_parent('Port-channel1')
Example 4 must return:
('lag', None)

Example 5 call: kind_and_parent('Vlan10')
Example 5 must return:
('vlan', None)

Example 6 call: kind_and_parent('BDI5')
Example 6 must return:
('vlan', None)

Example 7 call: kind_and_parent('Null0')
Example 7 must return:
('virtual', None)

Example 8 call: kind_and_parent('GigabitEthernet1/0/1')
Example 8 must return:
('physical', None)

Also write a unittest.TestCase with one test per example above,
asserting the return value is exactly equal (assertEqual).
```

#### Prompt C4: `looks_like_error(output)`

Goes in `scripts/drivers/ios.py`.

```text
Write one Python function for a read-only network data collector.
Use only the Python standard library. It must run on Python 3.9.
Do not print, log, or raise for unexpected input: return the empty
result described below instead.

def looks_like_error(output):
Return True if a Cisco device rejected the command instead of running it.
Look only at the first two lines of output.strip(). It is an error if
either line, with leading spaces removed, starts with one of "% Invalid",
"% Unrecognized", "% Unknown", "% Incomplete", "% Ambiguous",
"Unrecognized command", or if either line, stripped, is exactly "^".
Otherwise return False (also for empty output).

Example 1 call: looks_like_error("% Invalid input detected at '^' marker.\n")
Example 1 must return:
True

Example 2 call: looks_like_error("                 ^\n% Invalid input detected at '^' marker.\n")
Example 2 must return:
True

Example 3 call: looks_like_error('% Ambiguous command:  "show x"\n')
Example 3 must return:
True

Example 4 call: looks_like_error('Cisco IOS XE Software\n')
Example 4 must return:
False

Example 5 call: looks_like_error('')
Example 5 must return:
False

Also write a unittest.TestCase with one test per example above,
asserting the return value is exactly equal (assertEqual).
```

#### Prompt C5: `parse_show_version(text)`

Goes in `scripts/drivers/ios.py`.

```text
Write one Python function for a read-only network data collector.
Use only the Python standard library. It must run on Python 3.9.
Do not print, log, or raise for unexpected input: return the empty
result described below instead.

def parse_show_version(text):
Read Cisco "show version" output and return a dict with these keys, in
this order: "hostname", "os_version", "model", "serial", "role".
Each value is None when it isn't found. Use these regular expressions
(re.MULTILINE), taking group 1:
  hostname:   ^(\S+) uptime is
  os_version: ^Cisco IOS.*?Version ([^\s,]+)
  model:      ^cisco (\S+)(?: \([^)]*\))? .*?processor      (also re.IGNORECASE)
  serial:     ^Processor board ID (\S+)
role is "switch" if the model matches ^(WS-C|C9[2-6]\d\d|C1000|C2960|C3[68]50|IE-)
(re.IGNORECASE), otherwise "router" (also when model is None).
If text is None or empty, return all four values None and role "router".

Example 1 input (between the lines of ===):
=====
Cisco IOS XE Software, Version 17.18.02
Cisco IOS Software [IOSXE], Virtual XE Software (X86_64_LINUX_IOSD-UNIVERSALK9-M), Version 17.18.2, RELEASE SOFTWARE (fc3)
Technical Support: http://www.cisco.com/techsupport
Copyright (c) 1986-2025 by Cisco Systems, Inc.
Compiled Fri 19-Dec-25 03:49 by mcpre


ROM: IOS-XE ROMMON
AT-8K-1 uptime is 22 hours, 53 minutes
Uptime for this control processor is 22 hours, 55 minutes
System returned to ROM by reload
System image file is "bootflash:packages.conf"
Last reload reason: reload

cisco C8000V (VXE) processor (revision VXE) with 1651587K/3075K bytes of memory.
Processor board ID 9AAR82D4VBB
Router operating mode: Autonomous
5 Gigabit Ethernet interfaces

Configuration register is 0x2102
=====
Example 1 must return:
{'hostname': 'AT-8K-1',
 'os_version': '17.18.02',
 'model': 'C8000V',
 'serial': '9AAR82D4VBB',
 'role': 'router'}

Example 2 call: parse_show_version('')
Example 2 must return:
{'hostname': None, 'os_version': None, 'model': None, 'serial': None, 'role': 'router'}

Also write a unittest.TestCase with one test per example above,
asserting the return value is exactly equal (assertEqual).
```

#### Prompt C6: `parse_show_ip_interface(text)`

Goes in `scripts/drivers/ios.py`.

```text
Write one Python function for a read-only network data collector.
Use only the Python standard library. It must run on Python 3.9.
Do not print, log, or raise for unexpected input: return the empty
result described below instead.

def parse_show_ip_interface(text):
Read Cisco "show ip interface" output. Each interface starts with a header
line matching
  ^(?P<name>\S+) is (?P<admin>administratively down|up|down|deleted)\S*,\s+line protocol is (?P<proto>\w+)
and the following lines (until the next header) belong to it.
Return a dict {interface name: {"vrf": vrf, "ipv4": [addresses]}} in
output order, keys "vrf" then "ipv4".
- Each line containing "Internet address is A.B.C.D/N" or
  "Secondary address A.B.C.D/N" adds "A.B.C.D/N" to ipv4, in order.
- A line containing VPN Routing/Forwarding "NAME" sets vrf to NAME.
- vrf is "default" when there is no such line.
Lines before the first header are ignored. Empty text returns {}.

Example 1 input (between the lines of ===):
=====
GigabitEthernet1 is up, line protocol is up
  Internet address is 10.0.10.1/24
  Secondary address 10.0.11.1/24
  Broadcast address is 255.255.255.255
Vlan20 is up, line protocol is up
  Internet address is 10.20.0.1/24
  VPN Routing/Forwarding "MGMT"
Loopback0 is administratively down, line protocol is down
  Internet protocol processing disabled
=====
Example 1 must return:
{'GigabitEthernet1': {'vrf': 'default', 'ipv4': ['10.0.10.1/24', '10.0.11.1/24']},
 'Vlan20': {'vrf': 'MGMT', 'ipv4': ['10.20.0.1/24']},
 'Loopback0': {'vrf': 'default', 'ipv4': []}}

Also write a unittest.TestCase with one test per example above,
asserting the return value is exactly equal (assertEqual).
```

#### Prompt C7: `parse_show_interfaces(text)`

Goes in `scripts/drivers/ios.py`.

```text
Write one Python function for a read-only network data collector.
Use only the Python standard library. It must run on Python 3.9.
Do not print, log, or raise for unexpected input: return the empty
result described below instead.

def parse_show_interfaces(text):
Read Cisco "show interfaces" output into a dict {name: interface} in
output order. Interfaces start with the same header line as in
"show ip interface":
  ^(?P<name>\S+) is (?P<admin>administratively down|up|down|deleted)\S*,\s+line protocol is (?P<proto>\w+)
Each interface is a dict with exactly these keys in this order:
  "name": the name from the header
  "kind", "parent": from kind_and_parent(name) (already written, in this module)
  "mac": from "address is XXXX.XXXX.XXXX", as "xx:xx:xx:xx:xx:xx" lower case, else None
  "admin_up": True unless the header says "administratively down"
  "oper_up": True if the line protocol word is "up"
  "speed_mbps": from "BW <n> Kbit": n / 1000, as an int when it is a whole number, else a float; None if absent
  "mtu": from "MTU <n> bytes" as an int, else None
  "description": text after "Description:" (stripped), else ""
  "vrf": always "default"
  "vlan": from "Encapsulation 802.1Q Virtual LAN, Vlan ID <n>" as an int;
          else for kind "vlan" and a name like "Vlan30", 30; else None
  "ipv4": [], "ipv6": []
Port-channel members: a port-channel's block can have a line
"Members in this channel: Gi1/0/23 Gi1/0/24". After reading all
interfaces, set "parent" of each member to the port-channel's name. Member
names are abbreviated: resolve each with canonical_intf(member, list of
interface names) from drivers.common, and skip members not found.
Empty text returns {}.

Example 1 input (between the lines of ===):
=====
GigabitEthernet1/0/1 is up, line protocol is up (connected)
  Hardware is Gigabit Ethernet, address is 00a1.b2c3.0101 (bia 00a1.b2c3.0101)
  Description: to rtr1
  MTU 1500 bytes, BW 1000000 Kbit/sec, DLY 10 usec,
GigabitEthernet1/0/5 is up, line protocol is up (connected)
  Hardware is Gigabit Ethernet, address is 00a1.b2c3.0105 (bia 00a1.b2c3.0105)
  MTU 1500 bytes, BW 1000000 Kbit/sec, DLY 10 usec,
GigabitEthernet1/0/23 is up, line protocol is up (connected)
  Hardware is Gigabit Ethernet, address is 00a1.b2c3.0123 (bia 00a1.b2c3.0123)
  MTU 1500 bytes, BW 1000000 Kbit/sec, DLY 10 usec,
GigabitEthernet1/0/24 is up, line protocol is up (connected)
  Hardware is Gigabit Ethernet, address is 00a1.b2c3.0124 (bia 00a1.b2c3.0124)
  MTU 1500 bytes, BW 1000000 Kbit/sec, DLY 10 usec,
Port-channel1 is up, line protocol is up (connected)
  Hardware is Gigabit Ethernet, address is 00a1.b2c3.0123 (bia 00a1.b2c3.0123)
  Description: to sw2
  MTU 1500 bytes, BW 1000000 Kbit/sec, DLY 10 usec,
  Members in this channel: Gi1/0/23 Gi1/0/24 
Vlan10 is up, line protocol is up (connected)
  Hardware is Gigabit Ethernet, address is 00a1.b2c3.0010 (bia 00a1.b2c3.0010)
  MTU 1500 bytes, BW 1000000 Kbit/sec, DLY 10 usec,
=====
Example 1 must return:
{'GigabitEthernet1/0/1': {'name': 'GigabitEthernet1/0/1',
                          'kind': 'physical',
                          'mac': '00:a1:b2:c3:01:01',
                          'admin_up': True,
                          'oper_up': True,
                          'speed_mbps': 1000,
                          'mtu': 1500,
                          'description': 'to rtr1',
                          'vrf': 'default',
                          'vlan': None,
                          'parent': None,
                          'ipv4': [],
                          'ipv6': []},
 'GigabitEthernet1/0/5': {'name': 'GigabitEthernet1/0/5',
                          'kind': 'physical',
                          'mac': '00:a1:b2:c3:01:05',
                          'admin_up': True,
                          'oper_up': True,
                          'speed_mbps': 1000,
                          'mtu': 1500,
                          'description': '',
                          'vrf': 'default',
                          'vlan': None,
                          'parent': None,
                          'ipv4': [],
                          'ipv6': []},
 'GigabitEthernet1/0/23': {'name': 'GigabitEthernet1/0/23',
                           'kind': 'physical',
                           'mac': '00:a1:b2:c3:01:23',
                           'admin_up': True,
                           'oper_up': True,
                           'speed_mbps': 1000,
                           'mtu': 1500,
                           'description': '',
                           'vrf': 'default',
                           'vlan': None,
                           'parent': 'Port-channel1',
                           'ipv4': [],
                           'ipv6': []},
 'GigabitEthernet1/0/24': {'name': 'GigabitEthernet1/0/24',
                           'kind': 'physical',
                           'mac': '00:a1:b2:c3:01:24',
                           'admin_up': True,
                           'oper_up': True,
                           'speed_mbps': 1000,
                           'mtu': 1500,
                           'description': '',
                           'vrf': 'default',
                           'vlan': None,
                           'parent': 'Port-channel1',
                           'ipv4': [],
                           'ipv6': []},
 'Port-channel1': {'name': 'Port-channel1',
                   'kind': 'lag',
                   'mac': '00:a1:b2:c3:01:23',
                   'admin_up': True,
                   'oper_up': True,
                   'speed_mbps': 1000,
                   'mtu': 1500,
                   'description': 'to sw2',
                   'vrf': 'default',
                   'vlan': None,
                   'parent': None,
                   'ipv4': [],
                   'ipv6': []},
 'Vlan10': {'name': 'Vlan10',
            'kind': 'vlan',
            'mac': '00:a1:b2:c3:00:10',
            'admin_up': True,
            'oper_up': True,
            'speed_mbps': 1000,
            'mtu': 1500,
            'description': '',
            'vrf': 'default',
            'vlan': 10,
            'parent': None,
            'ipv4': [],
            'ipv6': []}}

Also write a unittest.TestCase with one test per example above,
asserting the return value is exactly equal (assertEqual).
```

#### Prompt C8: `parse_arp(text)`

Goes in `scripts/drivers/ios.py`.

```text
Write one Python function for a read-only network data collector.
Use only the Python standard library. It must run on Python 3.9.
Do not print, log, or raise for unexpected input: return the empty
result described below instead.

def parse_arp(text):
Read Cisco "show ip arp" output into a list of dicts with keys, in order,
"ip", "mac", "intf", "vrf". Only lines that (after strip()) match
  ^Internet\s+(\d+\.\d+\.\d+\.\d+)\s+\S+\s+([0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4})\s+\S+\s*(\S*)\s*$
count. mac is converted with norm_mac() from drivers.common; intf is the
last column or None when it is empty; vrf is always "default".
Empty text returns [].

Example 1 input (between the lines of ===):
=====
Protocol  Address          Age (min)  Hardware Addr   Type   Interface
Internet  10.0.10.1               -   5254.00aa.0001  ARPA   GigabitEthernet1
Internet  10.0.10.11             12   0050.5601.0011  ARPA   GigabitEthernet1
Internet  10.0.10.12              4   0050.5601.0012  ARPA   GigabitEthernet1
Internet  10.0.10.99             -   Incomplete      ARPA
=====
Example 1 must return:
[{'ip': '10.0.10.1',
  'mac': '52:54:00:aa:00:01',
  'intf': 'GigabitEthernet1',
  'vrf': 'default'},
 {'ip': '10.0.10.11',
  'mac': '00:50:56:01:00:11',
  'intf': 'GigabitEthernet1',
  'vrf': 'default'},
 {'ip': '10.0.10.12',
  'mac': '00:50:56:01:00:12',
  'intf': 'GigabitEthernet1',
  'vrf': 'default'}]

Also write a unittest.TestCase with one test per example above,
asserting the return value is exactly equal (assertEqual).
```

#### Prompt C9: `parse_mac_table(text)`

Goes in `scripts/drivers/ios.py`.

```text
Write one Python function for a read-only network data collector.
Use only the Python standard library. It must run on Python 3.9.
Do not print, log, or raise for unexpected input: return the empty
result described below instead.

def parse_mac_table(text):
Read Cisco "show mac address-table dynamic" output into a list of dicts
with keys, in order, "mac", "intf", "vlan". Use lines (not stripped)
matching
  ^\s*(\d+|All)\s+([0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4})\s+\S+\s+(?:\S+\s+)*?([A-Za-z][\w./:-]*\d)\s*$
Skip lines whose port (group 3), upper-cased, is CPU, DROP, ROUTER or
SWITCH. mac is norm_mac(group 2) from drivers.common, intf is group 3,
vlan is int(group 1), or None for "All". Empty text returns [].

Example 1 input (between the lines of ===):
=====
          Mac Address Table
-------------------------------------------

Vlan    Mac Address       Type        Ports
----    -----------       --------    -----
  10    5254.00aa.0001    DYNAMIC     Gi1/0/1
  10    0050.5601.0011    DYNAMIC     Gi1/0/5
  10    0050.5601.0012    DYNAMIC     Po1
  10    00a1.b2c4.0010    DYNAMIC     Po1
Total Mac Addresses for this criterion: 4
=====
Example 1 must return:
[{'mac': '52:54:00:aa:00:01', 'intf': 'Gi1/0/1', 'vlan': 10},
 {'mac': '00:50:56:01:00:11', 'intf': 'Gi1/0/5', 'vlan': 10},
 {'mac': '00:50:56:01:00:12', 'intf': 'Po1', 'vlan': 10},
 {'mac': '00:a1:b2:c4:00:10', 'intf': 'Po1', 'vlan': 10}]

Also write a unittest.TestCase with one test per example above,
asserting the return value is exactly equal (assertEqual).
```

#### Prompt C10: `parse_cdp_neighbors(text)`

Goes in `scripts/drivers/ios.py`.

```text
Write one Python function for a read-only network data collector.
Use only the Python standard library. It must run on Python 3.9.
Do not print, log, or raise for unexpected input: return the empty
result described below instead.

def parse_cdp_neighbors(text):
Read Cisco "show cdp neighbors detail" output into a list of neighbor
dicts. Entries are separated by lines made only of "-" characters.
In each entry, using stripped lines:
- "Device ID: X" -> remote_name X, with any trailing "(...)" removed
- the first "IP address: A" -> remote_ip A
- "Platform: P,  Capabilities: C1 C2" -> remote_platform P (stripped); the
  capability words map router->"router", switch->"bridge",
  trans-bridge->"bridge", source-route-bridge->"bridge", host->"station",
  phone->"phone" (compare lower-case, ignore others, no duplicates, keep order)
- "Interface: L,  Port ID (outgoing port): R" -> local_intf L, remote_intf R
  (the text after the first ":" in each half, stripped)
Skip an entry without both a Device ID and an Interface line.
Each neighbor has exactly these keys, in order: "local_intf", "protocol"
(always "cdp"), "remote_name", "remote_intf", "remote_mac" (always None),
"remote_ip", "remote_platform", "remote_capabilities" (a list).
Empty text returns [].

Example 1 input (between the lines of ===):
=====
-------------------------
Device ID: sw1.lab.local
Entry address(es): 
  IP address: 10.0.10.2
Platform: cisco C9300-24T,  Capabilities: Switch IGMP
Interface: GigabitEthernet1,  Port ID (outgoing port): GigabitEthernet1/0/1
Holdtime : 143 sec

Version :
Cisco IOS Software

advertisement version: 2
Duplex: full
Management address(es): 
  IP address: 10.0.10.2


Total cdp entries displayed : 1
=====
Example 1 must return:
[{'local_intf': 'GigabitEthernet1',
  'protocol': 'cdp',
  'remote_name': 'sw1.lab.local',
  'remote_intf': 'GigabitEthernet1/0/1',
  'remote_mac': None,
  'remote_ip': '10.0.10.2',
  'remote_platform': 'cisco C9300-24T',
  'remote_capabilities': ['bridge']}]

Also write a unittest.TestCase with one test per example above,
asserting the return value is exactly equal (assertEqual).
```

#### Prompt C11: `parse_lldp_neighbors(text)`

Goes in `scripts/drivers/ios.py`.

```text
Write one Python function for a read-only network data collector.
Use only the Python standard library. It must run on Python 3.9.
Do not print, log, or raise for unexpected input: return the empty
result described below instead.

def parse_lldp_neighbors(text):
Read Cisco "show lldp neighbors detail" output into a list of neighbor
dicts. Entries are separated by lines made only of "-" characters. In each
entry, split each stripped line at its first ":" into key and value
(value stripped) and use:
  "Local Intf" -> local, "Chassis id" -> chassis, "Port id" -> port,
  "Port Description" -> port_desc, "System Name" -> name,
  "Enabled Capabilities" -> a comma-separated list, each mapped
     R->"router", B->"bridge", T->"phone", W->"wlan", S->"station",
     C->"docsis", O->"other", P->"repeater" (no duplicates, keep order),
  the first "IP" -> ip.
Skip an entry with no "Local Intf". Servers usually send their NIC's MAC
as the port id, so: port_mac = norm_mac(port) (from drivers.common).
  remote_intf = port_desc if port_mac else port   (None if empty)
  remote_mac  = port_mac, or norm_mac(chassis) when port_mac is None
  remote_name = name, or None if empty or missing
Each neighbor has exactly these keys, in order: "local_intf", "protocol"
(always "lldp"), "remote_name", "remote_intf", "remote_mac", "remote_ip",
"remote_platform" (always None), "remote_capabilities".
Empty text returns [].

Example 1 input (between the lines of ===):
=====
------------------------------------------------
Local Intf: Gi1/0/23
Chassis id: 00a1.b2c4.0100
Port id: Gi1/0/23
Port Description: GigabitEthernet1/0/23
System Name: sw2.lab.local

System Description: 
Cisco IOS Software [Cupertino], Catalyst L3 Switch Software

Time remaining: 104 seconds
System Capabilities: B,R
Enabled Capabilities: B,R
Management Addresses:
    IP: 10.0.10.3
Auto Negotiation - supported, enabled
Vlan ID: - not advertised

------------------------------------------------
Local Intf: Gi1/0/24
Chassis id: 00a1.b2c4.0100
Port id: Gi1/0/24
Port Description: GigabitEthernet1/0/24
System Name: sw2.lab.local

System Description: 
Cisco IOS Software [Cupertino], Catalyst L3 Switch Software

Time remaining: 104 seconds
System Capabilities: B,R
Enabled Capabilities: B,R
Management Addresses:
    IP: 10.0.10.3
Auto Negotiation - supported, enabled
Vlan ID: - not advertised

------------------------------------------------
Local Intf: Gi1/0/5
Chassis id: 0050.5601.0011
Port id: 0050.5601.0011
Port Description: Ethernet0
System Name: srv1

Enabled Capabilities: S
Management Addresses:
    IP: 10.0.10.11

Total entries displayed: 3
=====
Example 1 must return:
[{'local_intf': 'Gi1/0/23',
  'protocol': 'lldp',
  'remote_name': 'sw2.lab.local',
  'remote_intf': 'Gi1/0/23',
  'remote_mac': '00:a1:b2:c4:01:00',
  'remote_ip': '10.0.10.3',
  'remote_platform': None,
  'remote_capabilities': ['bridge', 'router']},
 {'local_intf': 'Gi1/0/24',
  'protocol': 'lldp',
  'remote_name': 'sw2.lab.local',
  'remote_intf': 'Gi1/0/24',
  'remote_mac': '00:a1:b2:c4:01:00',
  'remote_ip': '10.0.10.3',
  'remote_platform': None,
  'remote_capabilities': ['bridge', 'router']},
 {'local_intf': 'Gi1/0/5',
  'protocol': 'lldp',
  'remote_name': 'srv1',
  'remote_intf': 'Ethernet0',
  'remote_mac': '00:50:56:01:00:11',
  'remote_ip': '10.0.10.11',
  'remote_platform': None,
  'remote_capabilities': ['station']}]

Also write a unittest.TestCase with one test per example above,
asserting the return value is exactly equal (assertEqual).
```

#### Prompt C12: `check_types(value, spec, path, problems)`

Goes in `scripts/check_facts.py`.

```text
Write one Python function for a read-only network data collector.
Use only the Python standard library. It must run on Python 3.9.
Do not print, log, or raise for unexpected input: return the empty
result described below instead.

def check_types(value, spec, path, problems):
Check that a JSON value (already loaded with json.load) has the shape
described by spec, and append a message to the list problems for every
mismatch. Never raise. path is a JSON path like "$.interfaces[0].mtu"
used at the start of each message. spec is one of:
- a dict {key: subspec}: value must be a dict. Each key of value that is
  not in spec -> f"{path}.{key}: unexpected key". Each key of spec missing
  from value -> f"{path}.{key}: missing", unless the key is in the
  module-level set OPTIONAL. Present keys are checked recursively with
  path f"{path}.{key}". If value is not a dict -> f"{path}: expected an object".
- the type dict itself (spec is dict): value must be a dict, contents unchecked.
- a list [subspec]: value must be a list (else f"{path}: expected a list");
  each item is checked with path f"{path}[{i}]".
- a type (str, int, float, bool) or a tuple of them that may also contain
  None, meaning null is allowed. If value is None and None isn't allowed:
  f"{path}: is null". bool is NOT an int: True for an int spec is wrong.
  On a type mismatch: f"{path}: expected {names joined by '/'}, got {type name}"
  where names are the allowed types' __name__ without None.
Example: with
  OPTIONAL = {"seconds"}
  SPEC = {"name": str, "mtu": (int, None), "up": bool, "ipv4": [str], "seconds": (int, float, None)}
  value = {"name": "Gi1", "mtu": "1500", "up": 1, "ipv4": ["10.0.0.1/30", 5], "extra": 0}
problems must become, in this order:
  ["$.extra: unexpected key", "$.mtu: expected int, got str",
   "$.up: expected bool, got int", "$.ipv4[1]: expected str, got int"]
and with value = {"name": "Gi1", "mtu": None, "up": True, "ipv4": []}
problems stays [].

Also write a unittest.TestCase with one test per example above,
asserting the return value is exactly equal (assertEqual).
```

### Extension example: the VLANs facet (section 5.3)

#### Prompt C13: `parse_vlan_brief(text)`

Goes in `scripts/drivers/ios.py`.

```text
Write one Python function for a read-only network data collector.
Use only the Python standard library. It must run on Python 3.9.
Do not print, log, or raise for unexpected input: return the empty
result described below instead.

def parse_vlan_brief(text):
Read Cisco "show vlan brief" output into a list of dicts with keys, in
order, "id" (int), "name", "ports" (list of port names as printed).
A VLAN line matches
  ^(\d+)\s+(\S+)\s+(active|act/\S+|suspended|sus\S*)\s*(.*)$
and group 4 holds ports separated by commas and/or spaces. A following line
that starts with a space and isn't blank continues the previous VLAN's
port list. Leave out VLANs 1002 to 1005. Empty text returns [].

Example 1 input (between the lines of ===):
=====

VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
1    default                          active    Gi1/0/2, Gi1/0/3, Gi1/0/4
10   SERVERS                          active    Gi1/0/1, Gi1/0/5
20   USERS                            active    
1002 fddi-default                     act/unsup 
1003 token-ring-default               act/unsup
=====
Example 1 must return:
[{'id': 1, 'name': 'default', 'ports': ['Gi1/0/2', 'Gi1/0/3', 'Gi1/0/4']},
 {'id': 10, 'name': 'SERVERS', 'ports': ['Gi1/0/1', 'Gi1/0/5']},
 {'id': 20, 'name': 'USERS', 'ports': []}]

Also write a unittest.TestCase with one test per example above,
asserting the return value is exactly equal (assertEqual).
```

#### Prompt C14: `parse_interfaces_trunk(text)`

Goes in `scripts/drivers/ios.py`.

```text
Write one Python function for a read-only network data collector.
Use only the Python standard library. It must run on Python 3.9.
Do not print, log, or raise for unexpected input: return the empty
result described below instead.

def parse_interfaces_trunk(text):
Read Cisco "show interfaces trunk" output into a list of dicts with keys,
in order, "port", "native" (int or None), "allowed", "active" (VLAN list
strings such as "1-4094" or "1,10,20" with spaces removed, "" for none).
The output has sections, each starting with a line beginning "Port":
the one containing "Native" gives each trunk's native VLAN (last column);
the one containing "allowed on trunk" gives allowed; the one containing
"active in" gives active; other sections are ignored. A trunk appears
first in the "Native" section; ignore ports not seen there. The word
"none" means "". Return trunks in the order of the "Native" section.

Example 1 input (between the lines of ===):
=====

Port        Mode             Encapsulation  Status        Native vlan
Po1         on               802.1q         trunking      1

Port        Vlans allowed on trunk
Po1         1-4094

Port        Vlans allowed and active in management domain
Po1         1,10,20

Port        Vlans in spanning tree forwarding state and not pruned
Po1         1,10,20
=====
Example 1 must return:
[{'port': 'Po1', 'native': 1, 'allowed': '1-4094', 'active': '1,10,20'}]

Also write a unittest.TestCase with one test per example above,
asserting the return value is exactly equal (assertEqual).
```
