# Stage 1 checklist: read-only data gathering in AWX

Created 2026-11-11 · Status: superseded by the [2026-11-14 revision](../2026-11-14/stage-1-awx-checklist.md) · Part of the [production roadmap](production-roadmap.md)

## What changed

This revises the [2026-11-08 checklist](../2026-11-08/stage-1-awx-checklist.md):

- Python 3.8 is now enough for preflight (production has 3.8.12 and OpenSSH 8.0p1).
- New section C: a dummy run of the whole job in AWX against simulated devices, before any real device is contacted. Later steps are renumbered.

## Overview

This proves two things on real devices: that AWX can log in to them over SSH, and that the JSON collection writes is correct. Before any real device, a dummy run proves the whole job works in AWX. It uses `playbooks/preflight.yml`, `playbooks/collect_facts.yml` and `dummy/inventory.yml` from this repository, unchanged. Only Cisco IOS / IOS-XE hosts are contacted.

Steps marked **(your environment)** need something only you have: AWX access, credentials, the inventory or the devices. Nothing in this checklist changes a device's configuration.

## A. Set up AWX (once)

1. **(your environment)** **Project.** Create or update the AWX project for this repository, on `main` after this change is merged. Sync it and note the revision AWX shows.
2. **(your environment)** **Credential.** Create a credential of type **Network** with the read-only collection account's username and password. Fill in "Authorize password" only if the account lands at a `>` prompt and needs `enable`.
3. **(your environment)** **Inventory.** Put one canary switch in the `network_devices` group, for example:

   ```yaml
   all:
     children:
       network_devices:
         hosts:
           sw-canary-1:                  # the device's hostname
             ansible_host: 10.1.2.3
             ansible_network_os: ios
   ```

   Leave out firewalls and Windows servers for now. If any are in the inventory, the job fails those hosts without contacting them.
4. **(your environment)** If the devices only offer old SSH algorithms, set `net_ssh_args` in `playbooks/group_vars/network_devices.yml` or as an inventory variable (see the README's SSH options).

## B. Preflight: no device contacted

5. **(your environment)** Create a job template for `playbooks/preflight.yml` with the inventory above. No credential is needed. Launch it.

   Pass when:
   - Python is 3.8 or later and an `OpenSSH_...` version is printed (production: 3.8.12 and OpenSSH_8.0p1);
   - the test result ends in `OK` (`OK (skipped=1)` is fine: the one skipped test needs LibreOffice);
   - the IOS plan lists exactly these 13 lines, and says `All 13 commands pass the read-only policy`:

     ```text
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
     ```

   If a test fails, stop and send me the `FAIL:` / `ERROR:` lines. Don't contact a device from an environment where the tests fail.

## C. Dummy run: no device contacted

6. **(your environment)** **Dummy inventory.** In AWX, create an inventory with a source "Sourced from a Project": this project, inventory file `dummy/inventory.yml`. It holds four simulated hosts (`sw1`, `sw2`, `rtr1`, `hq`). Each has `dummy_device` set, so the job runs it against `dummy/bin/ssh`, a script that plays a Cisco IOS device and makes no network connection. Their addresses are just their names and are never looked up.
7. **(your environment)** Create a job template for `playbooks/collect_facts.yml` with the dummy inventory. Attach any Network credential: a dummy device accepts any non-empty password, so a throwaway credential (`dummy` / `dummy`) is best. Add the extra variable `show_facts: true` and launch it.
8. Check the job log:
   - each host shows `connecting to <name>:22 as '<user>'` and one `sent` line per planned command, then `config-change check: unchanged`;
   - each host's `DUMMY DEVICE <name> (simulated, no real device contacted) received:` list is exactly the 13 planned lines from step 5, then `exit`. This is the dummy device's own record, standing in for device-side accounting;
   - `audit: 4 host(s), 56 line(s) sent in total, 0 failed`;
   - `CHECK PASS 4 file(s), 0 with problems, 4 with warnings only`. The warnings are expected: the saved output has no ARP table on the switches, no MAC table or VLANs on the routers, and so on;
   - the job ends green.

   Optional: uncomment `sw1-configchange` and `sw1-confirm` in `dummy/inventory.yml` (in a branch, or a copy of the inventory) and run again. Both must fail with `collect.py exited 3`: the first with `CONFIGURATION CHANGED DURING COLLECTION`, the second with `READ-ONLY SAFEGUARD STOPPED COLLECTION` and `ctrl-c` (never an answer) in its device log. The job then ends red, as it should.

   This tests the job, the credential, the collector, the SSH session, the safeguards and the checks in your AWX. It can't test the network path, SSH negotiation with a real device or real output: the canary does that.

## D. Canary: one switch

9. **(your environment)** Before running, if you can: turn on command accounting for the collection account (TACACS+ accounting, or `archive` / `log config` on the switch), so the device keeps its own record of what it received.
10. **(your environment)** Create a job template for `playbooks/collect_facts.yml` with the inventory and the Network credential attached. Set **Limit** to the canary's name. To see the JSON in the job log, add the extra variable `show_facts: true`. Launch it.
11. Check the job log, play by play:

   | Play | Look for | Pass |
   | --- | --- | --- |
   | Prepare output and check the read-only command plan | The plan | The same 13 lines as in step 5 |
   | Collect show-command output, read-only | `connecting to <address>:22 as '<user>'`, then one `sent` line per command | Every `sent` line is one of the 13 planned lines (plus `exit`, and `enable` first if used). `config-change check: unchanged` |
   | Audit the commands sent and check the JSON | `ok    <name>: 14 line(s) sent, config unchanged` (15 with `enable`) | `audit: 1 host(s), ..., 0 failed` |
   |  | `ok` or `warn  <name>: <model> <release>, N interfaces ...` | `CHECK PASS 1 file(s), 0 with problems` |

   The job ends green only if collection, the audit and the check all pass.
12. **(your environment)** **Compare with the device.** From the `show_facts` output, check a few values against the switch itself:
   - `device.hostname`, `model`, `os_version` and `serial` match `show version`;
   - the number of interfaces is about what `show ip interface brief` lists, and an interface you know has the right IPv4 address and up/down state;
   - `neighbors` matches `show cdp neighbors` / `show lldp neighbors`;
   - `mac_table` holds a MAC address you can see in `show mac address-table dynamic`.
13. **(your environment)** **Compare with the device's own record.** If accounting is on, the commands it logged for the collection account are exactly the `sent` lines in the job log, and nothing else.

## E. Widen

14. **(your environment)** Add one switch or router of each model and IOS release in the estate, and run step 10 with **Limit** covering them. Repeat steps 11 and 12 for each. Read every `warning:` line (see below).
15. **(your environment)** Remove the Limit to collect every IOS device. Pass: the audit and `CHECK PASS` for all of them, and every warning explained.

## Reading the result

| You see | It means | What to do |
| --- | --- | --- |
| `No device credentials in the job environment` | No Network credential on the job template | Attach it (step 10) |
| `authentication failed`, with what the device said | Wrong username or password, or the account isn't allowed SSH | Check the credential and the device's AAA |
| `ssh: connect to host ... Connection timed out` or `refused` | No network path from the execution environment, or SSH isn't on | Check routing, ACLs and `ip ssh` on the device |
| `no matching key exchange method` or `host key type` | The device offers only old algorithms | Set `net_ssh_args` (step 4) |
| `is panos, which is not enabled for this stage` | A firewall or server in the inventory | Expected; it wasn't contacted. Remove it or limit the job |
| `warning: no output kept for N planned command(s)` | The device rejected those commands (a router has no MAC table, LLDP is off) | Expected for those features; nothing to do |
| `warning: config-change marker could not be read` | The account can't run `show running-config` (below privilege 15) | Collection is fine, but that safeguard is lost. Raise the privilege, or accept it |
| `warning: ... name(s) not among the device's interfaces` or `parse_error` | Real output a parser reads wrongly | Send me the warning, the model and the release, and if possible the raw output of that command with names and addresses replaced. I fix the parser here first |
| `READ-ONLY SAFEGUARD STOPPED COLLECTION` or audit `FAIL` | A safeguard refused something | Stop. Send me the lines. Nothing refused was sent to the device |
| `CONFIGURATION CHANGED DURING COLLECTION` | The device's configuration-change marker moved during the run | Stop. Check the device's accounting for who changed what: collection sends only the lines above, so it may be another session. Send me the lines |
| `CHECK FAIL` with `problem:` lines | The JSON doesn't match the format | Send me the problem lines |

## What to send back

- From preflight: the Python and OpenSSH lines and the test result line.
- From the dummy run: the `audit:` line and the `CHECK` line.
- From each collection job: the `audit:` line, the `CHECK` line, and any `problem:` or `warning:` lines, with the model and IOS release for each warning.
- Whether the values in step 12 and the device's record in step 13 matched.

Replace any name or address production's rules don't allow to be shared.
