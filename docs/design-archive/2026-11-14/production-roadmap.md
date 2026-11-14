# Production roadmap

Created 2026-11-14 · Status: stage 1 passed on the dummy devices and on two development switches; stage 4 (the diagram) is ready to run, starting with a dummy run · Code: this repository, branch `main` once this change is merged

## What changed

This revises the [2026-11-11 production roadmap](../2026-11-11/production-roadmap.md) after stage 1 ran successfully in AWX, and Jon asked to move to stage 4 and publish the diagram to the internal Bitbucket instead of an artifact server.

- **Stage 4 is next, and it publishes to Bitbucket.** `gather_topology.yml` now clones a git repository, runs the stage 1 collection unchanged (`collect_facts.yml`, imported), builds the diagram, and commits `topology.vsdx`, `topology.drawio` and `template.vsdx` to that repository. The artifact server is gone from the playbooks and the inventory. Stages 2 and 3 (Palo Alto, Windows) follow later, as the earlier roadmap allowed.
- **The audit now gates the diagram.** The read-only audit and the JSON check run before the build. If either fails, nothing is built or published. Before, the audit ran last and the diagram was published anyway.
- **The layout lives in Bitbucket.** `template.vsdx`, which holds the node positions, is read from and written to the publish repository. This settles the 2026-11-11 question of where it lives in production.
- **Only a complete diagram is published.** If some inventory devices have no facts file from the run, the job builds the diagram, doesn't publish it, and fails, unless `bitbucket_publish_partial=true`.
- **A Visio file changes only when the diagram does,** so the repository gets a commit only when the topology or layout changed.
- **Credentials are no longer passed through a task's `environment:`.** Ansible puts those values on a command line, where they show in the process list and in the job log at verbosity 3. `collect.py` already read the credentials from the job's environment itself, so stage 1 behaves the same.
- **Stage 1 results so far.** The dummy run passed, and both development switches passed (`CHECK PASS`). One is a Catalyst 3850 on IOS-XE 03.07.05E; the other turned out to be a Catalyst 3750X on IOS 12.2(55)SE13, which needs `-o KexAlgorithms=+diffie-hellman-group1-sha1` in `net_ssh_args`. The configuration-change marker was unreadable on both, which is still open (see Open decisions).
- The dummy inventory is `dummy/inventory`, renamed on 2026-11-14 because AWX doesn't list `.yml` files as inventory sources.

Everything else is as in the 2026-11-11 version.

## Goal

Run autotop-awx on the production AWX, in stages, until production does everything `main` does: read-only collection from Cisco IOS / IOS-XE, Palo Alto firewalls and Windows servers; links from LLDP/CDP, MAC tables and shared subnets; the diagram in Visio and draw.io with its feature layers; and the read-only audit on every run.

Every stage keeps collection strictly read-only. The safeguards are described in the [README](../../../README.md#read-only-collection) and the [safeguards design](../2026-10-26/read-only-collection-safeguards.md); none of them are changed by this plan.

## How every stage runs

1. **Here:** any code a stage needs is changed in this repository, with tests, in a pull request. Tests must pass on Python 3.8 and later before it is merged.
2. **In AWX:** the project is synced to the merged commit. Jon notes the commit (AWX shows it on each job as the project revision), so a result is always tied to known code.
3. **Preflight** (`playbooks/preflight.yml`): runs the test suite on the production execution environment and prints the command plan. It contacts no device.
4. **Dummy run** (stages 1 and 4, and again after any change to collection): the whole job in AWX against simulated devices. It contacts no device.
5. **Live gates:** one canary device, then one of each model and software release, then all devices of the stage's platform. A gate that fails stops the rollout until the cause is understood.
6. **Feedback:** Jon sends back the lines listed under each stage (counts, check results, warnings). Anything that needs a fix is fixed here first and synced again. Production never runs code that isn't in this repository.

## Stages

| Stage | What production gains | Playbook | Code changes needed first | Read-only impact |
| --- | --- | --- | --- | --- |
| 0 | Prerequisites | none | none | A read-only account on the devices |
| 1 | Cisco IOS / IOS-XE collection: one checked JSON file per device | `collect_facts.yml` | Done. Passed on the dummy devices and two development switches | The 13 IOS lines below, nothing else |
| 2 | Palo Alto firewalls, directly or through Panorama | `collect_facts.yml` with `collect_platforms: [ios, panos]` | None known. Phase 3 was tested against a fake API only, so the first canary is the first real test | XML API: only `<show>` operations, one login per host |
| 3 | Windows servers | `collect_facts.yml` with `windows` added | None known. Tested against a fake OpenSSH server only | Two fixed, checked PowerShell scripts |
| 4 | The diagram, published to the internal Bitbucket | `gather_topology.yml` | Done in this change | None new: collection is stage 1's, unchanged; the build reads facts files only; git talks to Bitbucket only |
| 5 | Scheduled runs | `gather_topology.yml` on an AWX schedule | None | None new |

Stage 4 is next. Stages 2 and 3 come after it; nothing in stage 4 depends on them. Until then the diagram holds Cisco devices only, and `collect_platforms` keeps firewalls and servers from being contacted.

### Stage 0: prerequisites

Everything here is in Jon's environment.

- **AWX project** pointing at this repository, on the branch or tag production should run. Turn off "update revision on launch", or sync deliberately, so a job never picks up an unreviewed commit.
- **Execution environment** with Python 3.8 or later, the OpenSSH client (`ssh`) and ansible-core. Production has Python 3.8.12 and OpenSSH 8.0p1, which is enough. Stage 4 also needs `git`; preflight reports whether it is there. Nothing else is needed: the code uses only the standard library and `ansible.builtin`. The prebuilt C extension used from stage 4 is for Linux x86_64; on another architecture it needs one build (`python3 scripts/build_ext.py`). Preflight reports whether it loads.
- **Network path** from the execution environment to each device's SSH port (22, or `ansible_port`). Collection runs on the AWX side and connects out; Ansible itself never connects to the devices.
- **Read-only device account.** Best: TACACS+ command authorisation that permits only `show *` and `terminal length 0`, with command accounting on. At minimum, a local account that can run the planned commands. `show running-config | include configuration change` needs privilege 15 on most IOS releases. Without it the configuration-change check reports `unavailable` as a warning and collection still works, but that layer is lost.
- **AWX credential** of the built-in "Network" type for that account. If the account lands at a `>` prompt, fill in "Authorize password" for enable.
- **Inventory** with the stage's devices in `network_devices`, each with `ansible_host` and `ansible_network_os: ios` (or `iosxe`). Use the device's hostname as its inventory name: it names the facts file and, later, the diagram node.
- **Canary devices:** one switch to start, then one of each model and IOS release in the estate.
- **For stage 4:** a Bitbucket repository for the diagram, with a branch that has at least one commit; an account with write access to it and an HTTP access token; a network path from the execution environment to Bitbucket over HTTPS; and, if Bitbucket's certificate is signed by an internal CA the execution environment doesn't trust, that CA's certificate in a file the job can read.

### Stage 1: read-only data gathering, Cisco IOS / IOS-XE

The goal is to show that SSH works from AWX to the devices and that the JSON coming back is correct. Nothing else runs: no diagram, and nothing is copied off the job.

The step-by-step checklist is in [stage-1-awx-checklist.md](stage-1-awx-checklist.md).

**What runs.** `playbooks/collect_facts.yml`, three plays:

1. Print the read-only plan for each platform in the inventory, checked against the policy. A refused command stops the job before any device is contacted.
2. For each host: check its platform is in `collect_platforms` (default `[ios]`; a firewall or server in the inventory fails without being contacted), check a credential was attached, then run `scripts/collect.py`.
3. Run `audit.py` (every line sent re-checked against the policy, configuration unchanged) and `check_facts.py` (the JSON is right), and fail the job on any problem.

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
| G0 | `preflight.yml` | Python 3.8 or later, OpenSSH present, tests `OK`, the IOS plan is the 13 lines above | the Python and OpenSSH lines, the test result line |
| G1 | `collect_facts.yml` with `dummy/inventory`: no real device | All 4 dummy hosts: audit `ok`, config `unchanged`, `CHECK PASS`; each dummy device's log is exactly the 14 lines above. Optionally, the `configchange` and `confirm` dummy hosts fail with exit 3 | the audit and `CHECK` lines |
| G2 | `collect_facts.yml` limited to one canary switch | Audit `ok`, config `unchanged`, `CHECK PASS`; device-side accounting shows only the lines above; spot-checked values match the device | the audit line, the `CHECK` lines and any warning lines |
| G3 | One device of each model and IOS release | As G2 for each; any warning understood | the same, with model and release for each warning |
| G4 | All IOS devices | As G2; warnings explained | the summary lines |

**The dummy run.** G1 runs the real job in AWX, with the real credential plumbing, playbook, collector, SSH session and checks, against devices that don't exist. A host in `dummy/inventory` has `dummy_device` set, and the playbook then puts `dummy/bin/ssh` first on `PATH` for that host only. That script stands in for the OpenSSH client: it plays a Cisco IOS device in the pseudo-terminal (password prompt, `#` prompt, `--More--` pager, `% Invalid input` for unknown commands), answers from saved output of two Catalyst 9300 switches and two routers, and logs every line it receives. It opens no network connection. What it can't test is the network path, SSH negotiation with a real device, and real output, which is what the canary is for.

### Stages 2 and 3: Palo Alto and Windows

Both platforms are implemented, with their own read-only policies, and tested against fake devices. Neither has met a real device yet, so each starts at G0 again and goes straight to one canary (the dummy devices only play IOS).

- **Palo Alto** needs a read-only admin role on the firewall or Panorama, a custom AWX credential type that injects `PANOS_USERNAME` / `PANOS_PASSWORD` (or it uses the Network credential), and the firewall's CA certificate via `panos_ca_file`. Leave TLS verification on. Add `panos` to `collect_platforms`.
- **Windows** needs the OpenSSH server turned on, a non-administrator account, and a custom credential type for `WINDOWS_USERNAME` / `WINDOWS_PASSWORD`. Add `windows` to `collect_platforms`.

The README's Setup section has the details for both.

### Stage 4: the diagram, published to Bitbucket

The goal is a topology diagram of the collected devices, in Visio and draw.io, that outlives the AWX job. An AWX job's files are deleted with its container, so the job commits the diagram to a repository in the internal Bitbucket.

The step-by-step checklist is in [stage-4-awx-checklist.md](stage-4-awx-checklist.md).

**What runs.** `playbooks/gather_topology.yml`, six plays:

- **Play 1:** clone the publish repository (`bitbucket_repo_url`, branch `bitbucket_branch`). A wrong URL, branch or credential stops the job here, before any device is contacted.
- **Plays 2 to 4:** `collect_facts.yml`, exactly as in stage 1: the plan, the collection with its `collect_platforms` gate, then the audit and the JSON check. A failure in the audit or the check stops the job with nothing built or published.
- **Play 5:** build. `scripts/main.py` reads the facts files and the node positions in the repository's `template.vsdx`, and writes `topology.vsdx`, `topology.drawio` and the updated `template.vsdx` into the clone.
- **Play 6:** publish. Commit what changed, as `Topology from AWX job <id>`, and push.

**What stays read-only.** Collection is stage 1's, with every safeguard, and it is the only part that contacts a device. The build reads the facts files and writes diagram files. git talks only to Bitbucket.

**The credential.** `BITBUCKET_USERNAME` and `BITBUCKET_TOKEN`, from a custom AWX credential type (in the [README](../../../README.md#credentials)). git asks for them through `scripts/git_askpass.sh`, which prints them from the environment. They are never in the URL, on a command line, in git's configuration or in the job log; a URL with a password in it is refused.

**The layout.** `template.vsdx` in the published folder holds the node positions. Jon gets it from Bitbucket, moves the nodes in Visio, saves, and commits it back; the next job draws the diagram with those positions. The first run has no template, so every node starts in a staging grid.

**Only a complete diagram is published.** The diagram and the template hold only the devices collected in the run. If an inventory device has no facts file (it failed, its platform isn't in `collect_platforms`, or the job was limited), the job doesn't publish and fails, so a partial run can't replace the full diagram or lose positions. `bitbucket_publish_partial=true` overrides this.

**One folder per inventory.** The dummy devices, the development switches and production each need their own `bitbucket_path` (or branch), because each has its own devices and its own template.

**Gates.**

| Gate | What runs | Pass | Send back |
| --- | --- | --- | --- |
| H0 | `preflight.yml` | As G0, and the versions show git and `C extension .../_hanan.abi3.so` | the versions lines, the test result line |
| H1 | `gather_topology.yml` with `dummy/inventory` and `bitbucket_path: topology-dummy`: no real device | Audit `ok`, `CHECK PASS`, `Drew 4 nodes and 2 links`, `Published to ...`; the three files are in Bitbucket and `topology.vsdx` opens in Visio. A second run says `Nothing changed` | the `Drew` line, the `Published` line, and whether the file opens |
| H2 | The same with the development switches and `bitbucket_path: topology-dev` | As H1 for those devices; the link between the two switches is drawn if they are cabled together and run CDP or LLDP | the same, and whether the diagram matches the cabling |
| H3 | Move a node in `template.vsdx`, commit it, run again | The published diagram has the node where it was put | yes or no |
| H4 | Production inventory | As H2; decided when H1 to H3 have passed | the summary lines |

**What has been tested here, and what hasn't.** The whole job ran on Python 3.8 with ansible-core 2.13 against the dummy devices, publishing to a local git repository over HTTP with a user name and token: first publish, an unchanged second run, a moved node kept, a limited run refused, a wrong token, a missing branch and a URL with a password. It has not met Bitbucket itself, AWX's credential injection, or the production network path and certificate; H1 is the first test of those.

## Open decisions

- **SSH host keys are not verified.** `sshcli.py` runs `ssh` with `StrictHostKeyChecking=no` and no known-hosts file, so it would log in to whatever answers at a device's address. Inside a trusted management network that may be acceptable for stage 1. If production's security standard requires host key checking, it needs a small code change here (a known-hosts file option) before G2; `net_ssh_args` can't turn it on, because the built-in options come first and `ssh` keeps the first value it sees. I recommend deciding this before the canary.
- **Where facts files go.** AWX job containers are deleted after the job. `bitbucket_publish_facts=true` commits the JSON files to the publish repository under `facts/`. They hold each command's raw output, so decide whether that belongs in that repository before turning it on. Otherwise `show_facts=true` prints each file, without its raw output, in the job log.
- **The configuration-change marker is unreadable on both development switches.** Collection works, but that safeguard reports `unavailable` instead of `unchanged`. Jon is to run `show running-config | include configuration change` by hand as the collection account. `% Invalid input` means the account lacks the privilege for it; no output means these releases don't write that line, and the IOS driver then needs a different read-only marker.
- **Removed devices and the template.** A device removed from the inventory, or renamed, stops the build until its shape is deleted from `template.vsdx`. That was chosen so a typo in a hand-edited template isn't silently ignored. With the inventory changing in production it may be better to drop such shapes with a warning.

## What I need back from each job

Only what's needed to diagnose a problem:

- from preflight: the versions and the test result line;
- from collection: the `audit:` summary line, the `CHECK` line, and every `problem:` and `warning:` line;
- from the diagram job: the `Drew` line, the `feature` lines, the `Published` or `Not published` line, and the text of any failed task;
- for a parser warning or a `parse_error`: the device's model and IOS release, and if possible the raw output of the one command concerned with addresses and names replaced.

The per-device summary line names the device and its model. Leave out or replace anything production's rules don't allow to be shared.
