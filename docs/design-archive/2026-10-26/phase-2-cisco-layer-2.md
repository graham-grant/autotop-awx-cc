# Phase 2: Cisco layer 2

Created 2026-10-26 · Status: implemented · Phase 2 of the [multi-vendor device data design](multi-vendor-device-data-design.md), following [phase 1](phase-1-common-format.md)

## What was built

- **More of the core is filled in on IOS / IOS-XE.** Four core commands were added, all read-only and checked by `tests/test_readonly.py`:

  | Command | Fills |
  | --- | --- |
  | `show lldp neighbors detail` | `neighbors` (protocol `lldp`) |
  | `show cdp neighbors detail` | `neighbors` (protocol `cdp`) |
  | `show ip arp` | `arp` |
  | `show mac address-table dynamic` | `mac_table` |

  A router without a MAC address table, or a device with LLDP off, rejects the command. It is skipped like any rejected command and leaves that list empty.
- **Port-channel members.** `parent` on a physical port is now also set to its port-channel, from "Members in this channel" in `show interfaces`.
- **Shared helpers** (`drivers/common.py`): `norm_mac` writes every MAC as `aa:bb:cc:dd:ee:ff`, and `canonical_intf` expands abbreviated names (`Gi1/0/1`, `Po1`) against the device's own interface list, so neighbor, MAC table and VLAN entries use the same names as `interfaces`. Only an unambiguous match is expanded.
- **Link inference moved to `scripts/links.py`** and now uses evidence. `parser.py` only loads records and re-exports the link functions.
- **The `vlans` facet and the VLANs layer.** The IOS driver fills `facets.vlans` from `show vlan brief` and `show interfaces trunk`. `features/vlans.py` labels each switch end of a link with `VLAN 10` or `trunk 1,10,20`, on its own layer.
- **Rendering.** A link with only MAC table evidence is dashed (`config.INFERRED_LINK_STYLE`). A collapsed port-channel's interface labels read `Po1 ×2`.
- **Test fixtures** for layer 2 (`tests/fixtures/l2/`): a router, two switches on a two-member port-channel, and two servers known only from MAC tables, with `tests/test_l2.py`.

On the phase 1 lab fixtures, which have no layer 2 data, the diagram is byte-identical to the one drawn before this change except for one new, empty "VLANs" layer cell. Every link there still has `subnet` as its only evidence, and a test checks that.

## How links are built

Evidence is applied in this order, and each link keeps a list of the kinds that support it (`evidence`), plus `members` for port-channels.

1. **LLDP / CDP.**
   - The neighbor is matched to an inventory device by name (exact, or the FQDN's first label), then by management address, then by MAC.
   - The remote port is expanded against that device's interfaces. If it can't be placed (a server that sends its MAC as the port id), the MAC is looked up instead.
   - Reports from both ends become one link. Port-channel members collapse into one link between the two port-channels.
2. **MAC table.**
   - A switch port that has learned the MAC of exactly one other inventory device links the two.
   - A device's MAC owner is its physical interface where it has one, since SVIs and subinterfaces often reuse the base MAC.
   - Uplinks are ignored: ports whose LLDP/CDP neighbor is an inventory switch or advertises the bridge capability. A switch is a device with a MAC table, or role `switch`.
   - A port carrying the MACs of two or more inventory devices is ignored too. That catches an unmanaged switch, or a trunk to a switch that isn't in the inventory.
   - If one device appears on several qualifying ports, the port with the fewest MACs wins. A tie draws nothing and logs a note.
   - A pair already linked by LLDP/CDP gets no second link.
3. **Subnet.** The phase 1 rule, with one addition. If the subnet's members are already all joined by layer 2 links, directly or through switches that aren't members, no link or segment node is added. A direct layer 2 link between two members gains `subnet` as extra evidence, and their addresses fill in the link's IP labels.

`config.LINK_EVIDENCE` (default: all four kinds) says which kinds may draw a link. Taking out `mac` and `subnet` draws only cabling confirmed by LLDP/CDP.

## Where this differs from the design

**The evidence isn't in a tooltip yet.** The design said each link records its evidence "for the tooltip". The evidence is recorded on every link. On the diagram it shows only as the dashed style for MAC-only links, because adding a tooltip to physical links would change every existing link cell. A later change can add it on its own.

**Hiding inferred links is a setting, not a layer.** The design suggested a layer setting. Physical links all sit on the base layer, so the switch is `config.LINK_EVIDENCE` instead.

**ARP doesn't draw links.** The design lists ARP as an evidence source. Any IP neighbor that ARP proves is also in a subnet that the two devices share, so ARP adds no links that subnets don't. It's collected because servers and firewalls behind a router are identified by it in later phases.

**ARP is the global table only.** `show ip arp` doesn't include other VRFs. Collecting per-VRF tables needs `show ip arp vrf <name>` for each VRF, which means building commands from earlier output. The command plan would then no longer be fixed before connecting, and `collect.py --plan` couldn't show it in advance. It can be added later if needed, with each generated command still going through the policy.

**Switch stacks aren't collapsed.** On IOS-XE a stack is one device with one management address and one set of `show` outputs, so it's already one node, with member ports numbered `1/0/x`, `2/0/x`. Collapsing is only needed for platforms that report stack members as separate devices, and it can be added when one of those arrives.

**Trunk VLAN lists are stored as range strings** (`"1-4094"`, `"1,10,20"`), as the device prints them, not as lists of numbers. A trunk allowing all VLANs would otherwise be 4,094 numbers in every facts file.

## Unchanged

- Read-only safeguards. All new commands pass the IOS policy, and the policy itself didn't change.
- The playbook. The new commands come from the driver and the enabled features. A run against the fake device sends the new commands and the audit play passes.
- Node placement and link routing.

## Next

Phase 3 adds Palo Alto firewalls through the PAN-OS XML API, both directly (the lab) and through Panorama (production), with a read-only policy for API requests.
