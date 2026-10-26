# Phase 1: common device format and Cisco driver

Created 2026-10-26 · Status: implemented · Phase 1 of the [multi-vendor device data design](multi-vendor-device-data-design.md)

## What was built

- **`scripts/drivers/` replaces `scripts/netparse/`.** All vendor knowledge is in one driver per platform. The Cisco IOS / IOS-XE driver (`drivers/ios.py`) declares:
  - its transport (`ssh-cli`), vendor and commands;
  - its config-change marker and the commands behind each facet it supplies (`FACET_COMMANDS`);
  - `parse_core(raw)` and `parse_facets(raw)`.
- **Facts files are schema 2.** `collect.py` writes the parsed core (`device`, `interfaces`, `neighbors`, `arp`, `mac_table`), the facets, the raw output and the read-only audit into each `output/facts/<host>.json`.
- **Older files are upgraded on load.** Both the raw `"cli"` format and the NAPALM format are converted. `main.py --reparse` re-parses stored raw output with the current drivers.
- **IPsec is a facet.**
  - The Cisco driver parses `show crypto session detail` into `facets.ipsec`.
  - The IPsec feature reads only `topo.facet(host, "ipsec")`.
  - `Feature.commands` is gone. A feature declares `facet = "ipsec"`, and collection asks each driver for that facet's commands.
- **Shared segments.** A subnet with three or more members becomes a segment node linked to each member, instead of a full mesh.
- **VRFs on interfaces.** The IOS driver reads `VPN Routing/Forwarding "X"` from `show ip interface`.

On the existing lab fixtures, the diagram is byte-identical to the one drawn before this change, from both old-format and schema 2 facts files. A test checks this.

## Where this differs from the design

**VRF grouping only applies when an address is reused.** The design said to group subnets "by VRF and network together". Implementing that literally would break PE–CE links. VRF names only mean something on one device, so a PE's `CUST` VRF and its CE's global table share a /30 but have different VRF names.

The implemented rule:
- Group by network.
- Split a network's members by VRF name only when the same IP address appears on two devices in it. That is proof the subnet is reused, which is the VRF-lite or overlapping-customer case that would otherwise draw false links.
- Without reuse, VRF names are ignored.

**Segment nodes are positioned like devices.**
- A segment's id and its label in `template.drawio` is `segment <network>`, or `segment <vrf> <network>` when it was split by VRF.
- It can be dragged and pinned like any node. Labels starting with `segment ` are exempt from the inventory-name check, and stale ones are dropped.
- An unpinned segment is placed at the centroid of its members, moved down until it overlaps nothing.
- Its visible label is the network, prefixed with `VLAN <id> ·` when every member's interface is on the same VLAN.
- Links to a segment have no far-end interface label.

**Interface kinds.** `subinterface` was added to the design's list (physical, LAG, VLAN, loopback, tunnel, virtual), with `parent` set to the main interface and `vlan` from the 802.1Q encapsulation.

**Role.** The IOS driver sets `role` to `switch` for Catalyst models (WS-C, C9200 to C9600, C1000, C2960, C3650/3850, IE-) and to `router` otherwise. It isn't used for drawing yet. Icons still come from the inventory.

**Parser failures don't lose data.** If a driver raises, the file is still written with the raw output and a `parse_error`. The device is drawn with no links, and `--reparse` can repair it once the parser is fixed.

## Unchanged

- Read-only safeguards: every core, paging, config-check and facet command of every driver is checked by `tests/test_readonly.py`.
- The playbook, apart from comments.
- Link routing and rendering, apart from segment nodes.

## Next

Phase 2 (Cisco layer 2) fills `neighbors`, `arp` and `mac_table`. It also adds link evidence (LLDP/CDP before MAC table before subnet), LAG collapsing and the VLANs facet and layer.
