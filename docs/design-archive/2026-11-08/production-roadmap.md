# Production roadmap

Created 2026-11-08 · Status: proposed; stage 1 ready to run · Code: this repository, branch `main` once this change is merged

## What changed

This revises the [2026-10-31 production rebuild roadmap](https://github.com/graham-grant/autotop-awx-cc/pull/12) (on PR #12, never merged) and the 2026-11-03 manual rebuild guide that followed it.

- **No rebuild.** Microsoft Copilot is no longer part of the process. Production runs this repository's code as it is. The conformance kit, the Copilot prompts and the hand-written rebuild guide are all dropped: they existed only to prove that a second copy of the code behaved like this one, and there is no second copy now.
- **The stages enable features instead of writing them.** Every feature already exists and is tested here. Each stage turns one on in AWX and proves it on real devices, in the same gated way as before.
- **Validation is the code's own checks.** The project's test suite runs on the production execution environment (`playbooks/preflight.yml`), and every collection job audits what it sent (`audit.py`) and checks the JSON it wrote (the new `scripts/check_facts.py`).
- **Three small changes were made for stage 1:** a collection-only playbook (`playbooks/collect_facts.yml`) that builds no diagram and contacts only the platforms enabled for the stage; the facts checker; and the preflight playbook. A Python 3.9 bug was also fixed: `scripts/parser.py` is renamed `scripts/facts.py`, because on 3.9 `import parser` loads the standard library's own `parser` module instead, so `main.py` and 52 tests failed there.

## Goal

Run autotop-awx on the production AWX, in stages, until production does everything `main` does: read-only collection from Cisco IOS / IOS-XE, Palo Alto firewalls and Windows servers; links from LLDP/CDP, MAC tables and shared subnets; the diagram in Visio and draw.io with its feature layers; and the read-only audit on every run.

Every stage keeps collection strictly read-only. The safeguards are described in the [README](../../../README.md#read-only-collection) and the [safeguards design](../2026-10-26/read-only-collection-safeguards.md); none of them are changed by this plan.

## How every stage runs

1. **Here:** any code a stage needs is changed in this repository, with tests, in a pull request. Tests must pass on Python 3.9 and later before it is merged.
2. **In AWX:** the project is synced to the merged commit. Jon notes the commit (AWX shows it on each job as the project revision), so a result is always tied to known code.
3. **Preflight** (`playbooks/preflight.yml`): runs the test suite on the production execution environment and prints the command plan. It contacts no device.
4. **Live gates:** one canary device, then one of each model and software release, then all devices of the stage's platform. A gate that fails stops the rollout until the cause is understood.
5. **Feedback:** Jon sends back the lines listed under each stage (counts, check results, warnings). Anything that needs a fix is fixed here first and synced again. Production never runs code that isn't in this repository.

## Stages

| Stage | What production gains | Playbook | Code changes needed first | Read-only impact |
| --- | --- | --- | --- | --- |
| 0 | Prerequisites | none | none | A read-only account on the devices |
| 1 | Cisco IOS / IOS-XE collection: one checked JSON file per device | `collect_facts.yml` | Done in this change | The 13 IOS lines below, nothing else |
| 2 | Palo Alto firewalls, directly or through Panorama | `collect_facts.yml` with `collect_platforms: [ios, panos]` | None known. Phase 3 was tested against a fake API only, so the first canary is the first real test | XML API: only `<show>` operations, one login per host |
| 3 | Windows servers | `collect_facts.yml` with `windows` added | None known. Tested against a fake OpenSSH server only | Two fixed, checked PowerShell scripts |
| 4 | The diagram, published to the artifact server | `gather_topology.yml` | Make the artifact server's destination path a variable (it is hard-coded to a lab path today), and add `collect_platforms` there too | None new: the build reads facts files only |
| 5 | Scheduled runs | `gather_topology.yml` on an AWX schedule | None | None new |

Stages 2 and 3 can come after stage 4 if a diagram of the Cisco estate is wanted sooner. Nothing in stage 4 depends on them.

### Stage 0: prerequisites

Everything here is in Jon's environment.

- **AWX project** pointing at this repository, on the branch or tag production should run. Turn off "update revision on launch", or sync deliberately, so a job never picks up an unreviewed commit.
- **Execution environment** with Python 3.9 or later, the OpenSSH client (`ssh`) and ansible-core. Nothing else is needed: the code uses only the standard library and `ansible.builtin`. The prebuilt C extension used from stage 4 is for Linux x86_64; on another architecture it needs one build (`python3 scripts/build_ext.py`).
- **Network path** from the execution environment to each device's SSH port (22, or `ansible_port`). Collection runs on the AWX side and connects out; Ansible itself never connects to the devices.
- **Read-only device account.** Best: TACACS+ command authorisation that permits only `show *` and `terminal length 0`, with command accounting on. At minimum, a local account that can run the planned commands. `show running-config | include configuration change` needs privilege 15 on most IOS releases. Without it the configuration-change check reports `unavailable` as a warning and collection still works, but that layer is lost.
- **AWX credential** of the built-in "Network" type for that account. If the account lands at a `>` prompt, fill in "Authorize password" for enable.
- **Inventory** with the stage's devices in `network_devices`, each with `ansible_host` and `ansible_network_os: ios` (or `iosxe`). Use the device's hostname as its inventory name: it names the facts file and, later, the diagram node.
- **Canary devices:** one switch to start, then one of each model and IOS release in the estate.

### Stage 1: read-only data gathering, Cisco IOS / IOS-XE

The goal is to show that SSH works from AWX to the devices and that the JSON coming back is correct. Nothing else runs: no diagram, and nothing is copied off the job unless `artifact_facts_dir` is set.

The step-by-step checklist is in [stage-1-awx-checklist.md](stage-1-awx-checklist.md).

**What runs.** `playbooks/collect_facts.yml`, four plays:

1. Print the read-only plan for each platform in the inventory, checked against the policy. A refused command stops the job before any device is contacted.
2. For each host: check its platform is in `collect_platforms` (default `[ios]`; a firewall or server in the inventory fails without being contacted), check a credential was attached, then run `scripts/collect.py`.
3. Optional: copy the facts files to the artifact server.
4. Run `audit.py` (every line sent re-checked against the policy, configuration unchanged) and `check_facts.py` (the JSON is right), and fail the job on any problem.

**Exactly what an IOS device receives,** in this order, after the login answers:

```text
terminal length 0
show running-config | include configuration change
show version
show interfaces
show ip interface
show lldp neighbors detail
show cdp neighbors detail
show ip arp
show mac address-table dynamic
show crypto session detail
show vlan brief
show interfaces trunk
show running-config | include configuration change
exit
```

plus `enable` and its secret first, only if the account lands at a `>` prompt and an enable secret was given. A command the device rejects (no LLDP, no MAC table on a router) is skipped and leaves that part of the record empty.

**What makes it read-only.** Unchanged from `main`:

- The IOS policy allows only `show ...` with display filters, and `terminal length 0`, `terminal width N` and `terminal datadump`; it denies state-changing verbs and their abbreviations, file-writing pipes, control characters, newlines and `?`.
- `CliSession.run()` is the only path to the device and checks every command again as it is sent.
- A confirmation prompt is cancelled with Ctrl-C, never answered, and the host fails.
- The configuration-change marker is read before and after; a change fails the host with exit 3.
- After the run, `audit.py` re-checks every recorded line, and `check_facts.py` checks the lines sent were exactly the plan above, in order.

**What "correct JSON" means.** `check_facts.py` checks every file against the common device format (schema 2, in the [README](../../../README.md#device-facts-format)): every key present and no unexpected ones, every type, allowed values (interface kind, neighbor protocol, audit status), MAC addresses as `aa:bb:cc:dd:ee:ff`, addresses and prefixes that parse, and timestamps with a time zone. It also checks the device name matches the file and interface names are unique. It warns about commands the device didn't support, a parse error, an empty interface list, and neighbor, ARP or MAC entries on interface names the device doesn't have. The last of these is the main sign of real output that a parser misreads.

What the checker can't know is whether the values are true. That is Jon's check on the canary: compare a few values with the device itself (step 6 of the checklist).

**Gates.**

| Gate | What runs | Pass | Send back |
| --- | --- | --- | --- |
| G0 | `preflight.yml` | Python 3.9 or later, OpenSSH present, tests `OK`, the IOS plan is the 13 lines above | the Python and OpenSSH lines, the test result line |
| G1 | `collect_facts.yml` limited to one canary switch | Audit `ok`, config `unchanged`, `CHECK PASS`; device-side accounting shows only the lines above; spot-checked values match the device | the audit line, the `CHECK` lines and any warning lines |
| G2 | One device of each model and IOS release | As G1 for each; any warning understood | the same, with model and release for each warning |
| G3 | All IOS devices | As G1; warnings explained | the summary lines |

### Stages 2 and 3: Palo Alto and Windows

Both platforms are implemented, with their own read-only policies, and tested against fake devices. Neither has met a real device yet, so each starts at G0 again with one canary.

- **Palo Alto** needs a read-only admin role on the firewall or Panorama, a custom AWX credential type that injects `PANOS_USERNAME` / `PANOS_PASSWORD` (or it uses the Network credential), and the firewall's CA certificate via `panos_ca_file`. Leave TLS verification on. Add `panos` to `collect_platforms`.
- **Windows** needs the OpenSSH server turned on, a non-administrator account, and a custom credential type for `WINDOWS_USERNAME` / `WINDOWS_PASSWORD`. Add `windows` to `collect_platforms`.

The README's Setup section has the details for both.

### Stage 4: the diagram

`gather_topology.yml` collects, builds `topology.vsdx` and `topology.drawio`, publishes them and audits. Before it runs in production:

- its artifact server destination is hard-coded to a lab path (`/home/hp/Projects/autotop-awx/`), and needs to become a variable;
- it should get the same `collect_platforms` gate and preflight step as stage 1;
- the C extension must load on the execution environment (play 1 checks this, and preflight's tests cover it).

The layout is kept in `template.vsdx`, which Jon edits in Visio; where that file lives in production needs deciding then.

## Open decisions

- **SSH host keys are not verified.** `sshcli.py` runs `ssh` with `StrictHostKeyChecking=no` and no known-hosts file, so it would log in to whatever answers at a device's address. Inside a trusted management network that may be acceptable for stage 1. If production's security standard requires host key checking, it needs a small code change here (a known-hosts file option) before G1; `net_ssh_args` can't turn it on, because the built-in options come first and `ssh` keeps the first value it sees. I recommend deciding this before the canary.
- **Where facts files go.** AWX job containers are deleted after the job. If the JSON should be kept, set `artifact_facts_dir` and an `artifact_server` host. Otherwise `show_facts=true` prints each file, without its raw output, in the job log.

## What I need back from each job

Only what's needed to diagnose a problem:

- from preflight: the versions and the test result line;
- from collection: the `audit:` summary line, the `CHECK` line, and every `problem:` and `warning:` line;
- for a parser warning or a `parse_error`: the device's model and IOS release, and if possible the raw output of the one command concerned with addresses and names replaced.

The per-device summary line names the device and its model. Leave out or replace anything production's rules don't allow to be shared.
