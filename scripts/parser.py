"""
Reads the per-host facts files written by the playbook (schema 2, see
drivers/__init__.py; older files are upgraded on load) and derives links
from directly-connected subnets. Nothing here knows about drawio,
pathfinding, or rendering. Node positions are read from template.drawio by
drawio_position_loader.py.
"""
import ipaddress
import json
from collections import defaultdict
from pathlib import Path

import drivers
import inventory
from config import EXCLUDED_NETWORKS

SEGMENT_PREFIX = "segment "


def load_icons(inventory_path):
    """Map each inventory host name to its preferred icon library title.

    Reads inventory.yml directly rather than through the facts-JSON
    pipeline above, since `icon` is an inventory property, not something
    NAPALM reports. Supports a `vars: {icon: ...}` block at any group
    level (inherited by every host under it) as well as a per-host
    `icon:` key, with the host-level value winning if both are set.
    Hosts with no icon set anywhere are simply absent from the result.
    """
    return {name: hv["icon"] for name, hv in inventory.host_vars(inventory_path).items()
            if hv.get("icon")}


def load_nodes(facts_dir, reparse=False):
    """Read every *.json file in facts_dir as a schema 2 record. With
    reparse=True, each record is re-parsed from its raw output with the
    current drivers (applies a parser fix to old files)."""
    nodes = []
    for path in sorted(Path(facts_dir).glob("*.json")):
        with open(path) as f:
            record = json.load(f)
        nodes.append(drivers.reparse(record) if reparse else drivers.load(record))
    return nodes


def get_is_up(node, intf_name):
    """True/False if the device reported the interface's status, else None."""
    intf = drivers.interface(node, intf_name)
    if intf is None or intf.get("oper_up") is None:
        return None
    return bool(intf["oper_up"])


def extract_links(nodes):
    """Derive links by grouping every interface's IPv4 address by network.

    Two inventory hosts with an interface address in the same network are
    directly connected - this is exactly what a directly-connected/local
    route represents. A network with exactly two members becomes a link
    between them. One with three or more is a shared segment: it becomes a
    small segment node, with a link from each member to it, instead of a
    full mesh.

    VRFs: VRF names only mean something on one device (a PE's "CUST" VRF
    and its CE's global table share a /30), so members are grouped by VRF
    name only when the network shows reuse - the same address on two
    devices - which is when VRF-lite or overlapping customer space would
    otherwise draw false links.

    Returns (edges, segments). Each edge: id, source, target, source_intf,
    target_intf, source_ip, target_ip, is_up, and "segment" (the segment
    id, for links to a segment node). Each segment: id, label, network,
    vrf, members.
    """
    by_network = defaultdict(list)
    for node in nodes:
        name = drivers.name(node)
        for intf in node.get("interfaces", []):
            for cidr in intf.get("ipv4") or []:
                iface = ipaddress.ip_interface(cidr)
                network = iface.network
                if any(network.subnet_of(excluded) for excluded in EXCLUDED_NETWORKS):
                    continue
                by_network[network].append({
                    "node": name, "intf": intf["name"], "address": str(iface.ip),
                    "prefix": network.prefixlen, "vrf": intf.get("vrf") or "default",
                    "vlan": intf.get("vlan"),
                })

    nodes_by_name = {drivers.name(n): n for n in nodes}
    edges, segments = [], []

    def link_up(member):
        # Unknown status counts as up - a display simplification, not a
        # health signal. Only an explicit False brings a link down.
        return get_is_up(nodes_by_name[member["node"]], member["intf"]) is not False

    for network, members in by_network.items():
        groups = {None: members}
        addresses = [m["address"] for m in members]
        if len(set(addresses)) < len(addresses) and len({m["vrf"] for m in members}) > 1:
            groups = defaultdict(list)
            for m in members:
                groups[m["vrf"]].append(m)
            print(f"Note: {network} is reused in VRFs {sorted(groups)} - "
                  f"grouping its members by VRF.")

        for vrf, group in groups.items():
            # One entry per device, in case one reports the subnet on >1 interface (misconfig)
            deduped, seen = [], set()
            for m in group:
                if m["node"] not in seen:
                    seen.add(m["node"])
                    deduped.append(m)
            if len(deduped) < 2:
                continue  # no second inventory host on this subnet - nothing to draw

            if len(deduped) == 2:
                a, b = deduped
                edges.append({
                    "id": f"link_{len(edges)}",
                    "source": a["node"], "target": b["node"],
                    "source_intf": a["intf"], "target_intf": b["intf"],
                    "source_ip": f"{a['address']}/{a['prefix']}",
                    "target_ip": f"{b['address']}/{b['prefix']}",
                    "is_up": link_up(a) and link_up(b),
                })
                continue

            vlans = {m["vlan"] for m in deduped}
            label = str(network)
            if vrf:
                label = f"{vrf} · {label}"
            if len(vlans) == 1 and None not in vlans:
                label = f"VLAN {vlans.pop()} · {label}"
            seg_id = SEGMENT_PREFIX + (f"{vrf} {network}" if vrf else str(network))
            segments.append({"id": seg_id, "label": label, "network": str(network),
                             "vrf": vrf, "members": [m["node"] for m in deduped]})
            print(f"Note: {network} has {len(deduped)} inventory members "
                  f"({', '.join(m['node'] for m in deduped)}) - drawn as a segment node.")
            for m in deduped:
                edges.append({
                    "id": f"link_{len(edges)}",
                    "source": m["node"], "target": seg_id,
                    "source_intf": m["intf"], "target_intf": "",
                    "source_ip": f"{m['address']}/{m['prefix']}", "target_ip": str(network),
                    "is_up": link_up(m), "segment": seg_id,
                })
    return edges, segments
