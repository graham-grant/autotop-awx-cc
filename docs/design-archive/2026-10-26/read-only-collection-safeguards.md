# Read-only collection safeguards

Created 2026-10-26 · Status: implemented for Cisco IOS / IOS-XE · Applies to every platform added later

## Requirement

> It is critical that the data gathering step will NEVER alter a device's configuration. All commands executed must only be used to gather information.

There must also be a way to verify every command that was executed on real hardware.

## Summary

The guarantee is enforced in code at the point where text is written to the device, not left to whoever edits the command lists. There are four layers, and each would stop a mistake the others missed:

1. **Before connecting.** The full command plan for each platform is checked against that platform's read-only policy. If any command fails, or the platform has no policy, no device is contacted.
2. **As each command is sent.** `CliSession.run()` checks the command again and refuses it if the policy doesn't allow it. It never answers a confirmation prompt, and it records every line it types.
3. **Around the run.** The device's configuration-change marker is read before and after collection. If it moved, the host fails loudly.
4. **After the run.** `audit.py` re-reads the record of every line sent to every device, re-checks it against the policy independently, and fails the job if anything is wrong.

A test enumerates every command any driver or feature can emit and checks each one against its policy, so a command that isn't read-only can't be merged without a failing test.

## What can be sent to a device

Only three kinds of input ever reach a device:

| Input | Sent by | Why it's safe |
| --- | --- | --- |
| Login answers: username, password, `yes` to an SSH host-key question | `sshcli.open()`, only in response to the matching prompt | Authentication only. The host-key answer goes to the local `ssh` client, not the device. |
| `enable`, and the enable secret at its password prompt | `sshcli._enable()`, only if the device lands at a `>` prompt and an enable secret was given | Raises privilege for the session. Changes nothing on the device. Recorded in the audit. |
| Commands | `sshcli.run()`, only after the policy allows them | Checked by the policy below. Recorded in the audit. |
| `exit` | `sshcli.close()` | Ends the session. Recorded in the audit. |

Pager prompts (`--More--`) are answered with a space, which only shows the next page.

## The policy

`scripts/readonly.py` holds one `Policy` per platform. A command must pass all three rules.

**1. Allowlist.** The command must match one of the platform's allowed shapes. For IOS and IOS-XE:

| Allowed | Notes |
| --- | --- |
| `show <words>` | Words are letters, digits and `_ . / : -`. Abbreviations such as `sh` are refused, so the verb is always exactly `show`. |
| `show ... \| include/exclude/begin/section/count <text>` | Display filters only. More than one filter may be chained. |
| `terminal length 0`, `terminal width N`, `terminal datadump` | These change only the current session's display and are never saved. `terminal datadump` is the AT-8000S equivalent of `terminal length 0`. |

Refused even though it starts with `show`: `| redirect`, `| tee` and `| append`, because they write a file to the device's flash.

**2. Denylist.** If the first word is, or abbreviates, a verb that can change state on any supported platform, the command is refused even when an allowlist entry would let it through. This is a second line of defence against a mistake in an allowlist. The list includes `configure`, `write`, `copy`, `reload`, `delete`, `erase`, `format`, `clear`, `debug`, `no`, `default`, `crypto`, `license`, `install`, `request`, `test`, `send`, `boot`, `archive`, `event`, `tclsh`, `ping`, `telnet` and `ssh` for Cisco, `set`, `commit`, `load`, `save`, `revert` and `edit` for PAN-OS, and the PowerShell verbs `Set-`, `New-`, `Remove-`, `Add-`, `Enable-`, `Disable-`, `Restart-`, `Stop-`, `Start-`, `Invoke-` and others. Abbreviations are matched, so `conf t` and `wr` are refused.

**3. Characters.** Control characters, carriage returns, newlines, tabs and `?` are refused, along with leading, trailing or doubled spaces. A newline could add a second command. On IOS, `?` shows help and leaves the typed text in the line buffer, so the next command would be appended to it.

**Fail closed.** A platform with no policy gets nothing. `readonly.policy_for()` raises, so the plan check in play 1 stops the job and `collect.py` refuses to connect. Adding a platform means writing its policy first.

## Prompts and timeouts

A read-only command never asks for confirmation, so a confirmation prompt means something is wrong. If the output ends in `[confirm]`, `[yes/no]`, `(yes/no)`, `[y/n]`, `(y/n)` or `...]?` (for example, `Destination filename [startup-config]?`), the session:

1. sends Ctrl-C, never Enter, `y` or `yes`;
2. waits for the CLI prompt to come back;
3. marks the session as abandoned, so nothing else is sent;
4. raises `UnexpectedPrompt`, which fails the host with exit code 3.

A command that times out is also cancelled with Ctrl-C, and the session waits for the prompt before sending anything else. That stops the next command being taken as the answer to whatever the device was waiting for. If the prompt doesn't return, the session is abandoned.

## Verifying what was executed

Every line typed after login, other than credentials, is recorded with a UTC timestamp, its kind (`session`, `check` or `collect`) and its outcome (`ok`, `refused`, `timeout` or `cancelled`). The record appears in three places:

- **The job log, live.** Each line is printed as it is sent, as `<host>:     sent  show version`.
- **The facts file.** `output/facts/<host>.json` gets an `audit` section with the full record, the policy name and the configuration-check result. The facts are copied to the artifact server, so the record outlives the job.
- **The audit report.** Play 6 runs `audit.py`, which prints one line per host and a summary, and fails the job on any problem.

Before anything connects, play 1 prints the exact plan per platform, so the job log shows what will be sent before it is sent. Anyone can print the same plan offline:

```text
$ python3 scripts/collect.py --plan --platform ios
Read-only command plan for platform 'ios' (policy 'ios'):
  session  terminal length 0
  check    show running-config | include configuration change
  collect  show version
  collect  show interfaces
  collect  show ip interface
  collect  show crypto session detail
  check    show running-config | include configuration change
All 7 commands pass the read-only policy. Login answers, 'enable' and 'exit' are the only other input sent.
```

## Configuration-change check

IOS and IOS-XE record when the running configuration last changed, and who changed it, in a comment at the top of the running config:

```text
! Last configuration change at 09:10:00 UTC Mon Oct 26 2026 by admin
```

`show running-config | include configuration change` is read straight after paging is turned off, and again after the last command. The results are compared:

| Result | Meaning | Effect |
| --- | --- | --- |
| `unchanged` | The marker is identical | Normal |
| `changed` | The configuration changed while we were logged in | The facts file is written, then the host fails with exit code 3 and a `CONFIGURATION CHANGED DURING COLLECTION` message showing both markers. `audit.py` fails the job too. Because only allowed commands can be sent, the likely cause is another session, and the `by <user>` part says whose. |
| `unavailable` | The marker couldn't be read, for example because the account is below privilege 15 | A warning in the job log and the audit report. Doesn't fail the job. |
| `unsupported` | The platform has no marker command | Recorded. Doesn't fail the job. |

Each platform driver declares its own marker command (`CONFIG_CHECK_COMMAND`), and the command must pass the policy like any other.

## Tests

`tests/test_readonly.py` covers:

- **The policy.** A list of normal show commands is allowed, and more than 50 dangerous ones are refused, including abbreviations, file-writing pipes, embedded newlines, `?`, and PAN-OS and PowerShell verbs.
- **The command inventory.** Every platform driver has a policy. Every paging, core and config-check command of every driver, and every command of every feature, whether it is enabled or not, passes its platform's policy.
- **Enforcement in a live session.** These run against `tests/fake_ssh.py`, which logs every line it receives:
  - a refused command never reaches the device;
  - a session with no policy sends nothing;
  - a confirmation prompt is cancelled with Ctrl-C and never answered, and nothing is sent afterwards;
  - a timed-out command is cancelled before the next one is sent.
- **The audit.** The audit record matches, line for line, what the fake device received. A moving configuration marker fails the host with exit code 3. A refused feature command stops collection before any connection is made. `audit.py` passes clean records and fails records with a disallowed command, a missing audit or a changed configuration.

## Device-side controls (recommended)

The code above guarantees what this project sends. The device can guarantee what any session is allowed to do, which also covers a bug in this code. For production, I recommend:

- **Cisco:** a collection account whose privilege level, or TACACS+ command authorisation, permits only `show` and `terminal`. TACACS+ command accounting, or `archive log config` with `logging enable`, gives an independent record of every command that can be compared with the audit.
- **Palo Alto:** an admin account with a custom read-only role (Device or Panorama "Superuser (read-only)", or a role profile with only XML API operational and configuration read access), so `set`, `commit` and `request` are rejected by the firewall itself.
- **Windows:** a domain account that is not a local administrator, if the planned `Get-*` cmdlets return everything needed without administrative rights. This will be checked on the lab's Windows Server 2022 in phase 4, `Get-WindowsFeature` in particular.

## Policies for the planned platforms

These are written when each driver is added (phases 3 and 4 of the [multi-vendor design](multi-vendor-device-data-design.md)), and each needs the same tests.

**PAN-OS (XML API, direct or through Panorama).** Requests are URLs, not CLI text, so the policy checks the request parameters:

- `type=keygen` is allowed only as the first request.
- `type=op` is allowed only with a `cmd` whose XML root element is `<show>`.
- `type=config` is allowed only with `action=show` or `action=get`.
- Any other `type` (`commit`, `import`, `export`, `user-id`) is refused, as is `type=config` with `set`, `edit`, `delete`, `rename`, `move`, `clone` or `override`.
- Through Panorama, the same rules apply, and `target=<serial>` is the only extra parameter allowed.

**Windows (PowerShell over OpenSSH).** The collection script is a fixed file in the repository. Before connecting, the policy parses it and checks that:

- every command it calls is a `Get-*` cmdlet from an allowlist, or one of `ConvertTo-Json`, `Select-Object`, `Where-Object`, `ForEach-Object` or `Sort-Object`;
- it contains no `Invoke-Expression`, `iex`, `&` call operator, `.` dot-sourcing, `Start-Process` or .NET method calls that write.

It is then sent with `-EncodedCommand`, so the text that runs is exactly the text that was checked.
