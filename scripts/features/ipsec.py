"""
IPsec tunnels, drawn as curved dashed lines on their own layer.

Facts: each host's `show crypto session detail` output (gathered by the
playbook - see `commands` below), or, for hosts whose data comes from some
other collector, a pre-normalised list under the host JSON's "ipsec" key:

    "ipsec": {"tunnels": [{"interface": "Tunnel10", "local_address": "...",
                           "peer_address": "...", "status": "UP-ACTIVE",
                           "path": ["optional", "explicit", "hops"]}]}

Each tunnel's peer address is matched to whichever inventory host owns it;
both ends' reports are merged into one tunnel. The underlay path is the
explicit "path" if given, otherwise the fewest-hops path over the physical
links - the tunnel is drawn across every router on it. Tunnels terminating
on the same router converge into parallel lanes (see bundling.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from features import Feature
from features.bundling import plan_overlays

CMD = "show crypto session detail"


@dataclass
class Tunnel:
    id: str
    a: str
    b: str
    a_intf: str | None
    b_intf: str | None
    a_addr: str
    b_addr: str
    is_up: bool
    path: list[str]


# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------

_SA_RE = re.compile(r"IKEv[12] SA:\s+local\s+([0-9a-fA-F:.]+)/\d+\s+remote\s+([0-9a-fA-F:.]+)/\d+")
_FLOW_RE = re.compile(r"IPSEC FLOW:.*?host\s+([0-9a-fA-F:.]+)\s+host\s+([0-9a-fA-F:.]+)")


def parse_ios_crypto_session(text: str) -> list[dict]:
    """Cisco IOS / IOS-XE `show crypto session detail` -> tunnel records.

    Output is a series of blocks starting with "Interface: X"; each has a
    "Session status:" line, a "Peer: <addr> port ..." line and (when IKE
    is up) an "IKEv1/IKEv2 SA: local A/500 remote B/500" line giving the
    local address. A session with no IKE SA falls back to the IPSEC FLOW
    host pair, and failing that leaves local_address unset.
    """
    tunnels = []
    current = None

    def flush():
        if current and current.get("peer_address"):
            tunnels.append(current)

    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("Interface:"):
            flush()
            current = {"interface": s.split(":", 1)[1].strip(), "local_address": None,
                       "peer_address": None, "status": "UNKNOWN"}
        elif current is None:
            continue
        elif s.startswith("Session status:"):
            current["status"] = s.split(":", 1)[1].strip()
        elif s.startswith("Peer:") and current["peer_address"] is None:
            current["peer_address"] = s.split()[1]
        elif (m := _SA_RE.search(s)) and current["local_address"] is None:
            current["local_address"] = m.group(1)
            current["peer_address"] = current["peer_address"] or m.group(2)
        elif (m := _FLOW_RE.search(s)) and current["local_address"] is None:
            if m.group(2) == current["peer_address"]:
                current["local_address"] = m.group(1)
    flush()
    return tunnels


PARSERS = {"ios": parse_ios_crypto_session}


# ---------------------------------------------------------------------------
# Feature
# ---------------------------------------------------------------------------

class IpsecFeature(Feature):
    name = "ipsec"
    label = "IPsec tunnels"
    commands = {"ios": [CMD]}
    SETTINGS = {
        "up_color": "#7F00FF",
        "down_color": "#CC0000",
        "style": "curved=1;edgeStyle=none;html=1;dashed=1;dashPattern=8 4;"
                 "endArrow=none;startArrow=none;strokeWidth=2;jumpStyle=none;",
        "lane_spacing": 10.0,      # gap between converging parallel tunnels
        "converge_near": 30.0,     # parallel run starts this far from the node...
        "converge_far": 110.0,     # ...and lasts until this far out
        "transit_spacing": 8.0,    # sideways gap between tunnels over a transit router
    }

    def host_tunnels(self, topo, host) -> list[dict]:
        pre = (topo.hosts[host].get("ipsec") or {}).get("tunnels")
        if pre is not None:
            return pre
        parser = PARSERS.get(topo.platform(host))
        text = topo.cli(host, CMD)
        if parser is None or text is None:
            return []
        return parser(text)

    def collect(self, topo) -> list[Tunnel]:
        merged: dict[tuple, dict] = {}
        for host in sorted(topo.hosts):
            for rec in self.host_tunnels(topo, host):
                peer = topo.owner_of_ip(rec["peer_address"])
                if peer is None:
                    print(f"ipsec: {host} {rec.get('interface')}: peer {rec['peer_address']} "
                          f"isn't an inventory host - not drawn")
                    continue
                peer_host = peer[0]
                if peer_host == host:
                    continue
                local = rec.get("local_address") or "?"
                key = tuple(sorted([(host, local), (peer_host, rec["peer_address"])]))
                up = str(rec.get("status", "")).upper().startswith("UP")
                entry = merged.setdefault(key, {"intf": {}, "up": [], "path": None})
                entry["intf"][host] = rec.get("interface")
                entry["up"].append(up)
                if rec.get("path") and entry["path"] is None:
                    path = list(rec["path"])
                    entry["path"] = path if path[0] == key[0][0] else path[::-1]

        tunnels = []
        for (a_end, b_end), info in sorted(merged.items()):
            a, b = a_end[0], b_end[0]
            path = info["path"] or topo.shortest_path(a, b)
            if not path:
                print(f"ipsec: no physical path between {a} and {b} - tunnel not drawn")
                continue
            tunnels.append(Tunnel(
                id=f"t{len(tunnels)}", a=a, b=b,
                a_intf=info["intf"].get(a), b_intf=info["intf"].get(b),
                a_addr=a_end[1], b_addr=b_end[1],
                is_up=all(info["up"]), path=path,
            ))
        return tunnels

    def plan(self, tunnels, topo):
        geo = plan_overlays(
            {t.id: t.path for t in tunnels}, topo,
            spacing=self.settings["lane_spacing"],
            converge_near=self.settings["converge_near"],
            converge_far=self.settings["converge_far"],
            transit_spacing=self.settings["transit_spacing"],
        )
        return [(t, geo[t.id]) for t in tunnels if t.id in geo]

    def render(self, plan, layer):
        for t, g in plan:
            color = self.settings["up_color"] if t.is_up else self.settings["down_color"]
            tooltip = (f"IPsec {t.a} {t.a_intf or ''} ({t.a_addr}) <-> "
                       f"{t.b} {t.b_intf or ''} ({t.b_addr}) - {'up' if t.is_up else 'down'}\n"
                       f"via {' > '.join(t.path)}")
            layer.edge(t.id, g.source, g.target,
                       style=self.settings["style"] + f"strokeColor={color};",
                       points=g.points, exit=g.exit, entry=g.entry, tooltip=tooltip)


FEATURE = IpsecFeature()
