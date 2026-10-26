"""
IPsec tunnels, drawn as curved dashed lines on their own layer.

Facts: the "ipsec" facet of each host, which every driver that can supply
it fills in the same shape (the Cisco driver from `show crypto session
detail`; see scripts/drivers/):

    "facets": {"ipsec": {"tunnels": [{"interface": "Tunnel10", "local_address": "...",
                                      "peer_address": "...", "status": "up",
                                      "vendor_status": "UP-ACTIVE",
                                      "path": ["optional", "explicit", "hops"]}]}}

  interface       the device's own name for the tunnel (shown in the tooltip)
  local_address   this end's tunnel source address, or null if not reported
  peer_address    the far end's address (required)
  status          "up", "down" or "unknown", normalised by the driver
  vendor_status   the device's own status word, for the tooltip (optional)
  path            explicit underlay hops (optional)

Nothing here depends on the vendor at either end, so a tunnel from a Cisco
router to a Palo Alto firewall is drawn like any other. Each tunnel's peer
address is matched to whichever inventory host owns it; both ends' reports
are merged into one tunnel. An end that doesn't report its local address
takes it from the far end's report. The underlay path is the
explicit "path" if given, otherwise the fewest-hops path over the physical
links - the tunnel is drawn across every router on it. Tunnels terminating
on the same router converge into parallel lanes (see bundling.py).
"""
from __future__ import annotations

from dataclasses import dataclass

from features import Feature
from features.bundling import plan_overlays


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
# Feature
# ---------------------------------------------------------------------------

def _is_up(status) -> bool:
    """Facet status "up". Older pre-normalised data used the device's own
    word, so anything starting "up" (UP-ACTIVE, ...) counts too."""
    return str(status or "").lower().startswith("up")


class IpsecFeature(Feature):
    name = "ipsec"
    label = "IPsec tunnels"
    facet = "ipsec"
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
        return (topo.facet(host, self.facet) or {}).get("tunnels") or []

    def collect(self, topo) -> list[Tunnel]:
        reports = []  # (host, peer_host, record)
        for host in sorted(topo.hosts):
            for rec in self.host_tunnels(topo, host):
                peer = topo.owner_of_ip(rec["peer_address"])
                if peer is None:
                    print(f"ipsec: {host} {rec.get('interface')}: peer {rec['peer_address']} "
                          f"isn't an inventory host - not drawn")
                    continue
                if peer[0] != host:
                    reports.append((host, peer[0], rec))

        # What each host's tunnel peers say its address is, for ends that
        # don't report their own local address
        seen_as: dict[tuple, set] = {}
        for host, peer_host, rec in reports:
            seen_as.setdefault((peer_host, host), set()).add(rec["peer_address"])

        merged: dict[tuple, dict] = {}
        for host, peer_host, rec in reports:
            local = rec.get("local_address")
            if not local:
                guesses = seen_as.get((host, peer_host), set())
                local = next(iter(guesses)) if len(guesses) == 1 else "?"
            key = tuple(sorted([(host, local), (peer_host, rec["peer_address"])]))
            up = _is_up(rec.get("status"))
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
