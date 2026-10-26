"""
Link inference: which devices are connected, and by what evidence.

Links are built from several kinds of evidence, most trusted first:

  1. lldp / cdp  a neighbor entry names the exact cable: this port to that
                 port. Reports from both ends become one link.
  2. mac         a device's interface MAC was learned on a switch port, so
                 it hangs off that port. Uplinks are ignored (ports whose
                 neighbor is another switch, or that carry MACs of more than
                 one inventory device), or every device would attach to the
                 core.
  3. subnet      two devices with an address in the same IPv4 subnet are
                 directly connected (what a connected route means). A subnet
                 with three or more members becomes a segment node. A subnet
                 whose members are already joined by layer 2 links (directly,
                 or through switches) adds no new link: a direct link just
                 gains "subnet" as extra evidence.

ARP tables are collected (they're in the core) but don't add links of their
own: any IP neighbour that ARP proves is also in a shared subnet.

Port-channel members collapse into one link between the port-channels,
with a member count. Each link records its evidence, and
config.LINK_EVIDENCE says which kinds are drawn.

VRFs: VRF names only mean something on one device (a PE's "CUST" VRF and
its CE's global table share a /30), so a subnet's members are grouped by VRF
name only when the subnet shows reuse - the same address on two devices -
which is when VRF-lite or overlapping customer space would otherwise draw
false links.

extract_links(records) -> (edges, segments). Each edge: id, source, target,
source_intf, target_intf, source_ip, target_ip, is_up, evidence (list),
members (port-channel member count, 1 otherwise), and "segment" for links
to a segment node. Each segment: id, label, network, vrf, members.
"""
from __future__ import annotations

import ipaddress
from collections import defaultdict

import config
import drivers
from drivers.common import canonical_intf, norm_mac

SEGMENT_PREFIX = "segment "
L2_EVIDENCE = ("lldp", "cdp", "mac")


def get_is_up(node, intf_name):
    """True/False if the device reported the interface's status, else None."""
    intf = drivers.interface(node, intf_name)
    if intf is None or intf.get("oper_up") is None:
        return None
    return bool(intf["oper_up"])


def _first_ip(node, intf_name) -> str:
    intf = drivers.interface(node, intf_name) or {}
    return (intf.get("ipv4") or [""])[0]


class _Index:
    """Lookups over every inventory record: who owns a name, an IP or a
    MAC, and each device's interface names."""

    def __init__(self, records):
        self.by_name = {drivers.name(r): r for r in records}
        self.names: dict[str, str] = {}
        self.ip_owner: dict[str, str] = {}
        self.mac_owner: dict[str, tuple[str, str]] = {}
        for name, rec in self.by_name.items():
            for alias in (name, rec["device"].get("hostname")):
                if alias:
                    self.names.setdefault(alias.lower(), name)
                    self.names.setdefault(alias.lower().split(".")[0], name)
            # Prefer physical interfaces as a MAC's owner (subinterfaces and
            # SVIs often share the physical or base MAC)
            for intf in sorted(rec.get("interfaces", []),
                               key=lambda i: i.get("kind") != "physical"):
                for cidr in intf.get("ipv4") or []:
                    self.ip_owner.setdefault(cidr.split("/")[0], name)
                mac = norm_mac(intf.get("mac"))
                if mac:
                    self.mac_owner.setdefault(mac, (name, intf["name"]))

    def intf_names(self, host):
        return [i["name"] for i in self.by_name[host].get("interfaces", [])]

    def find_host(self, neighbor) -> str | None:
        name = (neighbor.get("remote_name") or "").lower()
        if name and (name in self.names or name.split(".")[0] in self.names):
            return self.names.get(name) or self.names[name.split(".")[0]]
        if neighbor.get("remote_ip") in self.ip_owner:
            return self.ip_owner[neighbor["remote_ip"]]
        mac = norm_mac(neighbor.get("remote_mac"))
        if mac in self.mac_owner:
            return self.mac_owner[mac][0]
        return None

    def parent(self, host, intf_name):
        """An interface's port-channel, or the interface itself."""
        intf = drivers.interface(self.by_name[host], intf_name) or {}
        return intf.get("parent") if intf.get("kind") != "subinterface" and intf.get("parent") \
            else intf_name

    def is_switch(self, host) -> bool:
        rec = self.by_name[host]
        return bool(rec.get("mac_table")) or rec["device"].get("role") == "switch"


def _neighbor_links(idx: _Index):
    """{frozenset of two (host, intf) ends: set of protocols}, from both
    ends' LLDP/CDP reports."""
    found: dict[frozenset, set] = {}
    for host, rec in idx.by_name.items():
        for n in rec.get("neighbors", []):
            remote = idx.find_host(n)
            if remote is None or remote == host or not n.get("local_intf"):
                continue
            remote_intf = canonical_intf(n.get("remote_intf"), idx.intf_names(remote))
            if remote_intf not in idx.intf_names(remote):
                # Port id we can't place (e.g. a MAC): pin it by MAC instead
                owner = idx.mac_owner.get(norm_mac(n.get("remote_mac")) or "")
                remote_intf = owner[1] if owner and owner[0] == remote else None
            if remote_intf is None:
                continue
            key = frozenset(((host, n["local_intf"]), (remote, remote_intf)))
            found.setdefault(key, set()).add(n.get("protocol") or "lldp")
    return found


def _uplinks(idx: _Index, neighbor_links) -> set:
    """(host, port) pairs that face another switch: their neighbor is an
    inventory switch, or reports the bridge capability."""
    up = set()
    for ends in neighbor_links:
        (a, ai), (b, bi) = sorted(ends)
        if idx.is_switch(b):
            up.add((a, idx.parent(a, ai)))
        if idx.is_switch(a):
            up.add((b, idx.parent(b, bi)))
    for host, rec in idx.by_name.items():
        for n in rec.get("neighbors", []):
            if "bridge" in (n.get("remote_capabilities") or []) and n.get("local_intf"):
                up.add((host, idx.parent(host, n["local_intf"])))
    return up


def _mac_links(idx: _Index, uplinks, linked_pairs):
    """[(switch, port, host, host_intf)] from MAC tables: a port that has
    learned the MAC of exactly one other inventory device, and isn't an
    uplink. If a device shows up on several such ports, the port with the
    fewest MACs wins (uplinks to unmanaged switches carry many)."""
    candidates = defaultdict(list)  # host -> [(total macs on port, switch, port, intf)]
    for switch, rec in idx.by_name.items():
        per_port = defaultdict(set)
        for entry in rec.get("mac_table", []):
            if entry.get("intf") and entry.get("mac"):
                per_port[idx.parent(switch, entry["intf"])].add(entry["mac"])
        for port, macs in per_port.items():
            if (switch, port) in uplinks:
                continue
            owners = {idx.mac_owner[m] for m in macs if m in idx.mac_owner}
            owners = {o for o in owners if o[0] != switch}
            if len({o[0] for o in owners}) != 1:
                continue
            host, host_intf = sorted(owners)[0]
            if frozenset((switch, host)) in linked_pairs:
                continue
            candidates[host].append((len(macs), switch, port, idx.parent(host, host_intf)))
    links = []
    for host, options in sorted(candidates.items()):
        options.sort()
        if len(options) > 1 and options[0][0] == options[1][0]:
            print(f"Note: {host}'s MAC is learned on {len(options)} access ports equally - "
                  f"not drawing a MAC-table link for it.")
            continue
        _, switch, port, host_intf = options[0]
        links.append((switch, port, host, host_intf))
    return links


def _l2_connected(idx: _Index, l2_pairs, members) -> bool:
    """Are all members joined by layer 2 links, directly or through
    switches that aren't members themselves?"""
    adj = defaultdict(set)
    for a, b in l2_pairs:
        adj[a].add(b)
        adj[b].add(a)
    members = set(members)
    start = next(iter(members))
    seen, stack = {start}, [start]
    while stack:
        here = stack.pop()
        for nxt in adj[here]:
            if nxt in seen:
                continue
            if nxt in members or idx.is_switch(nxt):
                seen.add(nxt)
                stack.append(nxt)
    return members <= seen


def extract_links(records):
    idx = _Index(records)
    allowed = set(getattr(config, "LINK_EVIDENCE", ("lldp", "cdp", "mac", "subnet")))
    edges, segments = [], []

    def link_up(host, intf):
        # Unknown status counts as up - a display simplification, not a
        # health signal. Only an explicit False brings a link down.
        return get_is_up(idx.by_name[host], intf) is not False

    def add_edge(a, ai, b, bi, evidence, members=1, a_ip=None, b_ip=None, segment=None):
        edge = {
            "id": f"link_{len(edges)}",
            "source": a, "target": b, "source_intf": ai, "target_intf": bi,
            "source_ip": a_ip if a_ip is not None else _first_ip(idx.by_name[a], ai),
            "target_ip": b_ip if b_ip is not None else
            (_first_ip(idx.by_name[b], bi) if b in idx.by_name else ""),
            "is_up": link_up(a, ai) and (b not in idx.by_name or link_up(b, bi)),
            "evidence": list(evidence), "members": members,
        }
        if segment:
            edge["segment"] = segment
        edges.append(edge)
        return edge

    # 1. LLDP / CDP, port-channel members collapsed
    neighbor_links = _neighbor_links(idx)
    grouped: dict[tuple, dict] = {}
    for ends, protocols in sorted(neighbor_links.items(), key=lambda kv: sorted(kv[0])):
        (a, ai), (b, bi) = sorted(ends)
        key = ((a, idx.parent(a, ai)), (b, idx.parent(b, bi)))
        g = grouped.setdefault(key, {"evidence": set(), "members": []})
        g["evidence"] |= protocols
        g["members"].append((ai, bi))
    l2_pairs = set()
    for ((a, ap), (b, bp)), g in grouped.items():
        evidence = [p for p in ("lldp", "cdp") if p in g["evidence"]]
        if not set(evidence) & allowed:
            continue
        add_edge(a, ap, b, bp, evidence, members=len(g["members"]))
        l2_pairs.add(frozenset((a, b)))

    # 2. MAC tables
    if "mac" in allowed:
        uplinks = _uplinks(idx, neighbor_links)
        for switch, port, host, host_intf in _mac_links(idx, uplinks, l2_pairs):
            add_edge(switch, port, host, host_intf, ["mac"])
            l2_pairs.add(frozenset((switch, host)))

    # 3. Shared subnets
    if "subnet" not in allowed:
        return edges, segments
    by_network = defaultdict(list)
    for rec in records:
        name = drivers.name(rec)
        for intf in rec.get("interfaces", []):
            for cidr in intf.get("ipv4") or []:
                iface = ipaddress.ip_interface(cidr)
                network = iface.network
                if any(network.subnet_of(excluded) for excluded in config.EXCLUDED_NETWORKS):
                    continue
                by_network[network].append({
                    "node": name, "intf": intf["name"], "address": str(iface.ip),
                    "prefix": network.prefixlen, "vrf": intf.get("vrf") or "default",
                    "vlan": intf.get("vlan"),
                })

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

            if l2_pairs and _l2_connected(idx, l2_pairs, [m["node"] for m in deduped]):
                # Layer 2 links already show how these connect; a direct
                # one just gains the subnet as evidence
                for i, m1 in enumerate(deduped):
                    for m2 in deduped[i + 1:]:
                        for e in edges:
                            if {e["source"], e["target"]} != {m1["node"], m2["node"]}:
                                continue
                            if "subnet" not in e["evidence"]:
                                e["evidence"].append("subnet")
                            for m in (m1, m2):
                                end = "source" if e["source"] == m["node"] else "target"
                                if e[f"{end}_intf"] == idx.parent(m["node"], m["intf"]):
                                    e[f"{end}_ip"] = e[f"{end}_ip"] or f"{m['address']}/{m['prefix']}"
                continue

            if len(deduped) == 2:
                a, b = deduped
                add_edge(a["node"], a["intf"], b["node"], b["intf"], ["subnet"],
                         a_ip=f"{a['address']}/{a['prefix']}",
                         b_ip=f"{b['address']}/{b['prefix']}")
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
                add_edge(m["node"], m["intf"], seg_id, "", ["subnet"],
                         a_ip=f"{m['address']}/{m['prefix']}", b_ip=str(network),
                         segment=seg_id)
    return edges, segments
