# Collection quickstart: the smallest version that runs on AWX

Written 2026-11-03 · A short companion to the [collection rebuild guide](collection-rebuild-guide.md), which holds the detail for later iterations

## What it does

For each device in the AWX inventory, it runs seven read-only `show` commands over SSH and writes one JSON file in the schema 2 format. Nothing is parsed yet: the command output is stored under `raw`, and the other sections stay empty. That's enough to show the job works, see real output, and start writing parsers.

## Execution flow

```text
AWX job template  (inventory + Network credential + playbooks/collect.yml)
 │
 │  for each device, on the job's own container (connection: local)
 ▼
collect.py --name sw1 --host 10.0.0.1 --platform ios --out /tmp/sw1.json
 │  1. picks the driver:  drivers/ios.py          (its COMMANDS list)
 │  2. reads the username and password that the Network credential put in the environment
 │  3. for each command in COMMANDS:
 │        sshexec.run_command()
 │          ├─ readonly.check()  ── refused? stop, nothing is sent
 │          └─ ssh user@device "show version"   (one login per command, input closed)
 │        output saved in record["raw"][command]
 │  4. driver.parse(raw)  ── fills interfaces, neighbors...  (empty for now)
 │  5. writes /tmp/sw1.json
 ▼
playbook prints the JSON in the job output
```

Only `sshexec.run_command()` talks to a device, and it calls `readonly.check()` first. A command is sent only if it's in the driver's list **and** has a read-only `show` shape.

## The files

Six files. Type them in as they are.

```text
scripts/readonly.py         the whitelist
scripts/sshexec.py          runs one command over SSH
scripts/drivers/__init__.py the list of drivers
scripts/drivers/ios.py      Cisco: the commands to run
scripts/collect.py          one device -> one JSON file
playbooks/collect.yml       the AWX job
```

### `scripts/readonly.py`

```python
"""The whitelist. Nothing is sent to a device unless check() allows it."""
import re

# A read-only command shape per platform: "show" + plain words,
# optionally one "| include/exclude/begin/section" filter.
SHAPES = {
    "ios": re.compile(r"show( [A-Za-z0-9._/:-]+)+"
                      r"( \| (include|exclude|begin|section)( [A-Za-z0-9._/:-]+)+)?"),
}


class CommandRefused(Exception):
    pass


def check(platform, command, allowed):
    shape = SHAPES.get(platform)
    if shape is None:
        raise CommandRefused(f"no whitelist for platform {platform!r}")
    if command not in allowed:
        raise CommandRefused(f"{command!r} is not in the driver's command list")
    if not shape.fullmatch(command):
        raise CommandRefused(f"{command!r} is not a read-only command")
```

### `scripts/sshexec.py`

ssh gets the password from a small throwaway script that reads it from an environment variable, so the password is never on a command line or in a file.

```python
"""Runs one whitelisted command on a device: one SSH login per command."""
import os
import stat
import subprocess
import tempfile

import readonly

# ssh asks this script for the password; it reads it from the environment,
# so the password is never on a command line or in a file.
ASKPASS = '#!/bin/sh\nprintf "%s\\n" "$AUTOTOP_SSH_PASSWORD"\n'


class SshError(Exception):
    pass


def run_command(host, username, password, platform, command, allowed, timeout=60):
    readonly.check(platform, command, allowed)      # raises before anything is sent
    with tempfile.TemporaryDirectory() as tmp:
        askpass = os.path.join(tmp, "askpass.sh")
        with open(askpass, "w") as f:
            f.write(ASKPASS)
        os.chmod(askpass, stat.S_IRWXU)
        env = dict(os.environ, SSH_ASKPASS=askpass, SSH_ASKPASS_REQUIRE="force",
                   DISPLAY=os.environ.get("DISPLAY") or "none",
                   AUTOTOP_SSH_PASSWORD=password)
        argv = ["ssh", "-T", "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
                "-o", "PubkeyAuthentication=no", "-o", "NumberOfPasswordPrompts=1",
                "-o", f"ConnectTimeout={timeout}", "-l", username, host, command]
        try:
            proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                  env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise SshError(f"{command!r} timed out")
    if proc.returncode == 255:                      # ssh itself failed
        msg = proc.stderr.decode("utf-8", "replace").strip()
        raise SshError(msg.replace(password, "****") if password else msg)
    return proc.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
```

### `scripts/drivers/__init__.py`

```python
from drivers import ios

DRIVERS = {"ios": ios}
```

### `scripts/drivers/ios.py`

```python
"""Cisco IOS / IOS-XE driver."""
PLATFORM = "ios"
VENDOR = "cisco"
COMMANDS = [
    "show version",
    "show interfaces",
    "show ip interface",
    "show lldp neighbors detail",
    "show cdp neighbors detail",
    "show ip arp",
    "show mac address-table dynamic",
]


def parse(raw):
    """raw = {command: output}. Returns the parts of the record it can fill.
    Iteration 1: nothing parsed yet; the raw output is kept in the JSON."""
    return {}
```

### `scripts/collect.py`

```python
#!/usr/bin/env python3
"""Collect one device and write one JSON file."""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

import drivers
import sshexec

ap = argparse.ArgumentParser()
ap.add_argument("--name", required=True)
ap.add_argument("--host", required=True)
ap.add_argument("--platform", required=True)
ap.add_argument("--out", required=True)
a = ap.parse_args()

driver = drivers.DRIVERS.get(a.platform.split(".")[-1])   # "cisco.ios.ios" -> "ios"
if driver is None:
    sys.exit(f"no driver for platform {a.platform!r}")
username = os.environ.get("ANSIBLE_NET_USERNAME")
password = os.environ.get("ANSIBLE_NET_PASSWORD")
if not username or not password:
    sys.exit("no credentials: attach a Network credential to the job template")

record = {
    "schema": 2,
    "device": {"name": a.name, "hostname": a.name, "role": None, "vendor": driver.VENDOR,
               "platform": driver.PLATFORM, "model": None, "os_version": None,
               "serial": None,
               "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "collector": "ssh-exec"},
    "interfaces": [], "neighbors": [], "arp": [], "mac_table": [], "facets": {},
    "raw": {}, "audit": {"commands": []},
}

for command in driver.COMMANDS:
    entry = {"command": command, "status": "ok"}
    record["audit"]["commands"].append(entry)
    try:
        record["raw"][command] = sshexec.run_command(
            a.host, username, password, driver.PLATFORM, command, driver.COMMANDS)
    except sshexec.SshError as e:
        entry["status"] = "error"
        print(f"{a.name}: {e}", file=sys.stderr)

parsed = driver.parse(record["raw"])
record["device"].update(parsed.pop("device", {}))
record.update(parsed)

with open(a.out, "w", encoding="utf-8") as f:
    json.dump(record, f, indent=2)
print(f"{a.name}: {len(record['raw'])} of {len(driver.COMMANDS)} commands collected")
if not record["raw"]:
    sys.exit(1)                     # nothing collected: fail this host in AWX
```

### `playbooks/collect.yml`

```yaml
---
- name: Collect read-only data from each device
  hosts: all
  connection: local          # collect.py does the SSH itself
  gather_facts: no
  tasks:
    - name: Run collect.py
      ansible.builtin.command: >-
        python3 {{ playbook_dir }}/../scripts/collect.py
        --name {{ inventory_hostname }}
        --host {{ ansible_host | default(inventory_hostname) }}
        --platform {{ ansible_network_os }}
        --out /tmp/{{ inventory_hostname }}.json
      changed_when: false

    - name: Show the JSON
      ansible.builtin.debug:
        msg: "{{ lookup('file', '/tmp/' + inventory_hostname + '.json') | from_json }}"
```

## Step 0: a job that only proves AWX runs

Before any device, commit this one file as `playbooks/hello.yml`, make a job template for it with any inventory, and launch it. It contacts nothing. It shows that the job runs, and that the execution environment has Python and an OpenSSH new enough for `sshexec.py` (8.4 or later).

```yaml
---
- name: Check the execution environment
  hosts: localhost
  connection: local
  gather_facts: no
  tasks:
    - name: Python and OpenSSH versions
      ansible.builtin.command: sh -c "python3 --version; ssh -V 2>&1"
      register: v
      changed_when: false
    - ansible.builtin.debug:
        var: v.stdout_lines
```

## Setting it up in AWX

1. Commit the six files to the Git repository AWX uses, and sync the **Project**.
2. **Credential:** type *Network*, with the collection account's username and password.
3. **Inventory:** add one test switch, with these host variables:
   ```yaml
   ansible_host: 10.0.0.1        # its management address
   ansible_network_os: ios
   ```
4. **Job template:** that inventory, that project, playbook `playbooks/collect.yml`, and the Network credential.
5. Launch it. The output shows `7 of 7 commands collected` and then the JSON.

If it fails:

| Message | Cause |
| --- | --- |
| `no credentials` | The Network credential isn't attached to the job template. |
| `Permission denied` straight away | Wrong password, or the execution environment's OpenSSH is older than 8.4 (`ssh -V`). |
| `Connection refused` or a timeout | The execution environment can't reach the device on port 22. |
| `% Invalid input` or `% Authorization failed` in `raw` | The account can't run that command without `enable`. Give it privilege 15, or a level that allows these `show` commands. |
| `is not a read-only command` | The whitelist refused a command. Nothing was sent. Fix the command; never widen the pattern. |

## What comes next, one step at a time

1. **Parse `show version`** into `device` (model, version, serial). Write `parse_show_version` with Copilot (prompt C5 in the guide), and have `parse()` return `{"device": {...}}`.
2. **Interfaces:** prompts C1, C2, C3, C6 and C7.
3. **Neighbors, ARP and MAC table:** prompts C8 to C11.
4. **The JSON checker** (`check_facts.py`, prompt C12), so the job fails when a file doesn't fit the format.
5. **More devices.**

Each step changes only `drivers/ios.py`, apart from the checker. The commands, the whitelist and the transport stay as they are.

## What was tested

The six files were run on Python 3.9 and ansible-core 2.15 against a test SSH server that answers like a Cisco switch. The test covered the full playbook, a wrong password, and the whitelist refusing `clear counters`, `conf t`, `show run | redirect flash:x` and a command with a newline in it. It hasn't been run against a real Cisco device yet.
