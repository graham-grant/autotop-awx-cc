# Phase 4: Windows servers

Created 2026-10-26 · Status: implemented, tested against a fake OpenSSH server, lab server verification pending · Phase 4 of the [multi-vendor device data design](multi-vendor-device-data-design.md), following [phase 3](phase-3-palo-alto.md)

## What was built

- **The Windows read-only policy** (`readonly.PowerShellPolicy`), as set out in the [read-only safeguards](read-only-collection-safeguards.md). A command can only name a fixed script in `scripts/drivers/powershell/`, as `powershell:<name>.ps1`, optionally pinned to its SHA-256 (`powershell:<name>.ps1#sha256=...`). The policy reads the script from the repository and checks it token by token:
  - every command in it is on an allowlist of read-only cmdlets (`Get-CimInstance`, `Get-NetAdapter`, `Get-NetIPAddress`, `Get-NetNeighbor`, `Get-NetLbfoTeam`, `Get-NetLbfoTeamMember`, `Get-WindowsFeature`) or the formatting cmdlets (`Select-Object`, `Where-Object`, `ForEach-Object`, `Sort-Object`, `ConvertTo-Json`);
  - `Where-Object` and `ForEach-Object` only take a script block, since `ForEach-Object Name` with a bare member name can call a method;
  - assignment is only to plain variables, never to an object's property;
  - there is no call operator `&`, dot-sourcing, method call, static member (`::`), type literal other than `[ordered]` and `[pscustomobject]`, double-quoted or here-string (both can run code inside `$(...)`), subexpression, redirection, backtick, block comment, or any character outside that plain subset.

  A pinned hash that no longer matches the file is refused. `tests/test_readonly.py` checks both scripts pass, and has about 45 refused commands and scripts.
- **A PowerShell-over-OpenSSH session** (`sshcli.PowerShellSession`), with the same interface, policy guard and audit as `CliSession`.
  - `run()` gets the script's text from the policy, so the text checked is the text sent. It is sent as `powershell -NoProfile -NonInteractive -EncodedCommand <base64>`, one SSH connection per script, with `-T` (no terminal on the server).
  - The password is answered once, at the SSH prompt. Nothing else is ever typed: the output is read until the connection closes.
  - The audit records each script as `powershell:<name>.ps1#sha256=<hash>`. The job log shows the hash and the encoded length.
  - Scripts print their JSON between `AUTOTOP-BEGIN` and `AUTOTOP-END` lines, and only that part is used, so a banner, a profile or PowerShell's progress records (CLIXML) around it don't matter.
- **Two scripts**: `windows_core.ps1` (computer, OS and BIOS, NICs, addresses, ARP cache and LBFO teams as one JSON document) and `windows_roles.ps1` (installed roles, for the `server` facet).
- **The Windows driver** (`drivers/windows.py`):

  | Source | Fills |
  | --- | --- |
  | `Win32_ComputerSystem`, `Win32_OperatingSystem`, `Win32_BIOS` | `device` (role `server`, vendor `microsoft`), and the domain in the `server` facet |
  | `Get-NetAdapter` | `interfaces` (MAC, state, speed, MTU, VLAN ID) |
  | `Get-NetIPAddress` | addresses on each interface; loopback and link-local IPv4 (APIPA) are left out |
  | `Get-NetNeighbor` | `arp`, without unreachable, incomplete, broadcast and multicast entries |
  | `Get-NetLbfoTeam` | NIC teams: the team is an interface of kind `lag`, and its NICs have it as their `parent` |
  | `Get-WindowsFeature` | `server` facet: installed roles |

  Windows PowerShell 5.1 writes enums as numbers in JSON and PowerShell 7 as names, so both are accepted.
- **Server placement by switch MAC tables.** No change to `links.py` was needed. The driver gives each NIC and team its MAC, and the existing `mac` evidence links a server to the access port where a switch learned it. A team is one interface with one MAC, so a teamed server is one link, to the team.
- **A server roles layer** (`features/roles.py`): a badge under each server with its well-known roles (`AD DS · DNS`), and every role and the domain in the badge's tooltip.
- **Separate Windows credentials, optionally.** `WINDOWS_USERNAME` and `WINDOWS_PASSWORD` take precedence for Windows hosts when set, as `PANOS_*` do for firewalls.
- **The playbook** passes the `WINDOWS_*` variables to `collect.py`. The credential check is now per host, so a job whose Windows servers have their own credential doesn't also need a Network credential for them. Play 1 prints the Windows script plan, and play 6 audits it.
- **Tests:** `tests/fake_winssh.py` stands in for `ssh` to a Windows server. It asks for the password, decodes the `-EncodedCommand`, logs the script it received, and answers with the fixture output for that script. `tests/test_windows.py` covers:
  - the driver, including PowerShell 7's enum names;
  - each script fitting on a `cmd.exe` command line once encoded;
  - the server receiving exactly the script the policy checked, with its hash in the audit;
  - refused commands, and a session with no or the wrong policy, never connecting;
  - a wrong password failing without the password appearing in the error;
  - a full `collect.py` run whose audit `audit.py` accepts, and an audit that fails when the recorded hash doesn't match the script;
  - a teamed server linked to the switch port that learned the team's MAC;
  - the roles badge and tooltip on the diagram.

A full playbook run against the fake Windows server, two fake firewalls and the fake IOS devices passed the audit. No password appeared in the job log or any facts file.

On the phase 1 lab fixtures, the diagram is byte-identical apart from one more empty layer cell, "Server roles".

## Where this differs from the design

**A custom credential type, not a Machine credential.** The design suggested an AWX Machine credential for the Windows account. A Machine credential sets Ansible's own connection user, which the job also uses to reach the artifact server, and a job template takes only one. A custom type that injects `WINDOWS_USERNAME` and `WINDOWS_PASSWORD` keeps the two apart, in the same way as the Palo Alto credential. Without it, servers use the Network credential.

**Two scripts, not one.** The design pictured one collection script. The roles list is a separate script because it belongs to the `server` facet: with the roles layer turned off, it isn't sent at all, in the same way as other facets' commands. It's also the one most likely to need administrator rights, so keeping it separate means a refusal there can't cost the network data.

**Stricter than planned.** The safeguards document listed the cmdlets to allow and the constructs to refuse. The policy goes further: every method call is refused, not only ones that write, and so are double-quoted strings, subexpressions and property assignment. A script that needs one of those has to be rewritten without it.

**Roles are a badge, not a node tooltip and a services layer.** The design put roles in the node's tooltip. A tooltip on the node itself would be part of the base diagram, which only draws the core, so the roles are on their own layer as a badge with its own tooltip. Services aren't collected yet.

**One collect task, not one per inventory group.** As for PAN-OS, the driver decides the transport from `ansible_network_os: windows`, so the playbook needs no `windows_servers` group. The example inventory uses one to set the platform once.

**No configuration-change marker.** The design didn't name one for Windows, and there is no single "last changed" value for a server's configuration. The check is recorded as `unsupported`. The guard is the fixed, checked scripts, and the hash in the audit shows which text ran.

## Open points

- **Get-WindowsFeature rights.** It may need a local administrator. If the account can't run it, the roles list comes back empty and the host gets no badge. The network data doesn't depend on it. This is checked on the lab's Server 2022 first; the safeguards document recommends a non-administrator account.
- **Switch Embedded Teaming.** Hyper-V `SET` teams (`Get-VMSwitchTeam`) aren't read, only LBFO teams, so a SET team's NICs appear as separate interfaces. That needs the Hyper-V cmdlets added to the allowlist, and a host to test against.
- **Command-line length.** OpenSSH on Windows runs the remote command through `cmd.exe`, which allows 8,191 characters. The core script is about 3,200 once encoded. The tests fail if a script grows past 8,000, which would be the time to split it.
- **LLDP on servers.** If a server does run LLDP (some NIC drivers and Hyper-V hosts can), its neighbors aren't read yet. MAC table placement covers the lab and production.

## To verify on the lab server

The parsers follow the documented cmdlet output, but no real Windows server has been contacted yet. On the lab's Windows Server 2022:

1. Enable OpenSSH Server (the README has the three commands), and check `ssh <user>@<server>` reaches it.
2. `python3 scripts/collect.py --plan --platform windows` shows the scripts, then run the playbook against the server and the switch it plugs into.
3. Compare its facts file with the fixture. The JSON from `Get-NetAdapter` and `Get-NetLbfoTeam` is the most likely to differ.
4. Check whether the roles list is filled with the account you intend to use in production.
5. If a field differs, fix the parser and its fixture, then `python3 scripts/main.py output/facts output --reparse`. No new collection is needed.

## Next

Phase 5 (scale) is deferred, as the design decided: production is one site, so the work that matters is keeping one large page fast and small.
