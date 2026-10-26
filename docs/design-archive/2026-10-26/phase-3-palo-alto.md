# Phase 3: Palo Alto firewalls

Created 2026-10-26 · Status: implemented, tested against a fake XML API, lab firewall verification pending · Phase 3 of the [multi-vendor device data design](multi-vendor-device-data-design.md), following [phase 2](phase-2-cisco-layer-2.md)

## What was built

- **The PAN-OS read-only policy** (`readonly.XmlApiPolicy`), as set out in the [read-only safeguards](read-only-collection-safeguards.md). A request is the query string of its API parameters, and the policy checks those, not CLI text:
  - `type=op` only with a `cmd` that parses as one XML element tree rooted at `<show>`;
  - `type=config` only with `action=get` or `show` under `/config`;
  - `type=keygen` alone;
  - `target=<serial>` as the only extra parameter.

  Everything else is refused, including repeated parameters, DTDs, entities, comments and processing instructions. `tests/test_readonly.py` has 10 allowed and more than 50 refused examples.
- **An XML API session** (`scripts/xmlapi.py`, standard library only). Its interface is the same as `sshcli.CliSession`, so `collect.py` treats both alike.
  - `run()` checks every request against the policy before sending it, and records it in the audit either way.
  - It sends exactly the parameters the policy checked, as a POST body. The API key is added after the check.
  - Login is one `type=keygen` with the credentials in the POST body. The key is kept in memory and masked in any error.
  - Certificates are verified by default, against the system store or `panos_ca_file`. `panos_verify_tls: false` is a per-host opt-out that logs a warning.
  - Plain http is refused except to localhost.
- **Direct and through-Panorama collection**, chosen per host by `panos_via_panorama`, `panorama_host` and `panos_serial`, as the design decided.
  - Through Panorama, every request carries `target=<serial>`, which the policy checks too.
  - When `panos_serial` isn't set, the serial is looked up with `show devices connected` on Panorama itself, matching the inventory name to the firewall's hostname. Hundreds of production firewalls then need no serial numbers in the inventory.
- **The PAN-OS driver** (`drivers/panos.py`):

  | Request | Fills |
  | --- | --- |
  | `show system info` | `device` (role `firewall`) |
  | `show interface all` | `interfaces` (zone, virtual router as `vrf`, VLAN tag from `<ifnet>`; MAC, state and speed from `<hw>`), and the `zones` facet |
  | `show arp entry name='all'` | `arp` |
  | `show lldp neighbors all` | `neighbors` |
  | `show vpn flow` | `ipsec` facet |
  | `show jobs all` | the config-change marker |

- **A zones layer** (`features/zones.py`): each firewall end of a link is labelled with its interface's zone, each zone in its own colour. Tagging link ends moved into a shared base (`features/porttags.py`) that the VLANs layer now uses too. The VLANs output is unchanged.
- **Separate firewall credentials, optionally.** `PANOS_USERNAME` and `PANOS_PASSWORD` take precedence for PAN-OS hosts when set, so firewalls can use their own read-only admin account.
- **The playbook** passes the `panos_*` variables to `collect.py`. Play 1 prints the PAN-OS request plan next to the IOS command plan, and play 6 audits both.
- **Tests:** `tests/fake_panos.py` serves the fixture XML as a firewall or as Panorama and records every request it receives. `tests/test_panos.py` covers:
  - the driver;
  - refused requests never reaching the device;
  - the key and password never appearing in the audit or log;
  - certificate checks with a throwaway self-signed certificate;
  - Panorama targeting and serial lookup;
  - a config change failing the host;
  - the zones layer;
  - a mixed diagram, in which a firewall cabled to the layer 2 lab's switch is linked by LLDP, labelled with its zone, and has an IPsec tunnel to the Cisco router.

A full playbook run against two fake firewalls (one direct over verified HTTPS, one through Panorama with verification off) and the fake IOS device passed the audit. No password or API key appeared in the job log or any facts file.

On the phase 1 lab fixtures, the diagram is byte-identical apart from two empty layer cells, "VLANs" and "Firewall zones".

## Where this differs from the design

**The config-change marker is the newest commit job.** The design didn't name one for PAN-OS, which has no "last changed" line. A firewall's running configuration only changes through a commit, from the GUI, CLI, API or a Panorama push, and every commit is a job. The marker is the highest-numbered job of a commit type in `show jobs all`, so other jobs finishing during collection (content updates, reports) don't count as a change. Uncommitted candidate edits aren't detected, because they don't change what the firewall is running. The read-only admin role recommended in the safeguards document stops them at the source.

**IPsec comes from `show vpn flow`, not `show vpn ipsec-sa` and `show vpn ike-sa`.** One request gives each tunnel's tunnel interface, local and peer address and state, which is everything the facet needs. PAN-OS says `active` for an established tunnel and `init` for one still negotiating. These map to `up` and `down`, and the word is kept as `vendor_status`.

**The facet is `zones`, not `firewall`.** Each other facet is named after the one layer that draws it (`ipsec`, `vlans`), and zones are the only firewall data drawn so far. Security policy or NAT can get facets of their own later.

**Zones are labels, not shaded groups.** The design pictured zones as shaded areas around their interfaces. On this diagram an interface exists only as a link end, so there is nothing to shade around. Each firewall link end gets a coloured zone label instead. A port whose subinterfaces are in several zones lists all of them.

**No `firewalls` group is required.** Firewalls go under `network_devices` like any device, with `ansible_network_os: panos`. A `firewalls` child group is the natural place to set that once, and the example inventory shows one, but the code doesn't depend on the group's name.

**Configuration reads aren't used yet.** Everything needed came from operational `show` commands, so no `type=config` request is sent. The policy allows `get` and `show` for later use.

**HA pairs are drawn as two firewalls.** Badging an HA pair as one node is left for when the lab or production has one to test against.

## Found along the way

**Staged nodes could overlap.** New devices with no position in `template.drawio` are placed in a staging grid every 200 points, but the default node is 230 points wide. With six new devices, overlapping nodes walled in a port and the router found no route, so the build failed. The grid now spreads out to fit the largest staged node plus `NEW_NODE_GRID_MIN_GAP` (80). Diagrams with pinned positions are unaffected.

## To verify on the lab firewall

The parsers follow the documented XML API output, but no real PAN-OS device has been contacted yet. On the lab firewall:

1. `python3 scripts/collect.py --plan --platform panos` shows the requests, then run the playbook against the firewall.
2. Compare its facts file with the fixture. The LLDP and `show jobs all` field names are the most likely to differ between PAN-OS versions.
3. If a field differs, fix the parser and its fixture, then `python3 scripts/main.py output/facts output --reparse`. No new collection is needed.

## Next

Phase 4 adds Windows Server over PowerShell and OpenSSH, with a `server` facet and server placement by MAC table.
