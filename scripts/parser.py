"""
Reads the per-host JSON files written by the Ansible playbook (raw CLI
output, normalized by netparse) and derives
links from directly-connected subnets. Nothing here knows about drawio,
pathfinding, or rendering. Node positions are no longer read here - see
drawio_position_loader.py, which reads them from template.drawio instead.
"""
import ipaddress
import json
from collections import defaultdict
from pathlib import Path

import inventory
import netparse
from config import EXCLUDED_NETWORKS


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


def load_nodes(facts_dir):
    """Read every *.json file in facts_dir into a list of node dicts.

    Raw-CLI records from the playbook are normalized into the NAPALM-shaped
    schema (hostname / interfaces / interfaces_ip) by netparse; records
    already in that shape pass through unchanged.
    """
    nodes = []
    for path in sorted(Path(facts_dir).glob("*.json")):
        with open(path) as f:
            nodes.append(netparse.normalize(json.load(f)))
    return nodes


def get_is_up(node, intf_name):
    """Return True/False if NAPALM reported interface status, else None if unknown."""
    intf_data = (node.get("interfaces") or {}).get(intf_name)
    if intf_data is None or "is_up" not in intf_data:
        return None
    return bool(intf_data["is_up"])


def extract_edges(nodes):
    """Derive links by grouping every interface's IPv4 address by network.

    Two inventory hosts with an interface address in the same network are
    directly connected - this is exactly what a directly-connected/local
    route represents, so no LLDP or routing-table lookups are needed.

    Returns a list of dicts: id, source, target, source_intf, target_intf,
    source_ip, target_ip, is_up.
    """
    by_network = defaultdict(list)
    for node in nodes:
        name = node["inventory_name"]
        for intf_name, ip_data in (node.get("interfaces_ip") or {}).items():
            for address, info in (ip_data.get("ipv4") or {}).items():
                prefix = info.get("prefix_length")
                if prefix is None:
                    continue
                network = ipaddress.ip_network(f"{address}/{prefix}", strict=False)
                if any(network.subnet_of(excluded) for excluded in EXCLUDED_NETWORKS):
                    continue
                by_network[network].append({
                    "node": name,
                    "intf": intf_name,
                    "address": address,
                    "prefix": prefix,
                })

    nodes_by_name = {n["inventory_name"]: n for n in nodes}
    edges = []
    for network, members in by_network.items():
        # De-dupe in case one node reports the same subnet on >1 interface (misconfig).
        deduped = []
        seen_node_names = set()
        for m in members:
            if m["node"] in seen_node_names:
                continue
            seen_node_names.add(m["node"])
            deduped.append(m)
        members = deduped

        if len(members) < 2:
            continue  # no second inventory host on this subnet - nothing to draw
        if len(members) > 2:
            print(f"Note: {network} has {len(members)} inventory members "
                  f"({', '.join(m['node'] for m in members)}) - drawing as a full mesh; "
                  f"this looks like a shared segment rather than a point-to-point link.")

        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                a_node, b_node = nodes_by_name[a["node"]], nodes_by_name[b["node"]]
                a_up = get_is_up(a_node, a["intf"])
                b_up = get_is_up(b_node, b["intf"])
                # Treat unknown status as up - this is a display simplification,
                # not a real health signal. Only an explicit False brings it down.
                is_up = (a_up is not False) and (b_up is not False)
                edges.append({
                    "id": f"link_{len(edges)}",
                    "source": a["node"],
                    "target": b["node"],
                    "source_intf": a["intf"],
                    "target_intf": b["intf"],
                    "source_ip": f"{a['address']}/{a['prefix']}",
                    "target_ip": f"{b['address']}/{b['prefix']}",
                    "is_up": is_up,
                })
    return edges
