"""
VLANs: labels each switch port on the diagram with what it carries, on its
own layer - "VLAN 20" for an access port, "trunk 10,20,30" for a trunk.

Facts: the "vlans" facet, which every driver that can supply it fills in
the same shape (the Cisco driver from `show vlan brief` and `show
interfaces trunk`; see scripts/drivers/):

    "facets": {"vlans": {
        "vlans":  [{"id": 20, "name": "USERS", "ports": ["GigabitEthernet1/0/5"]}],
        "trunks": [{"port": "Port-channel1", "native": 1,
                    "allowed": "1-4094", "active": "1,10,20"}]}}

  vlans[].ports     access ports in the VLAN (interface names as in the core)
  trunks[].allowed  VLANs allowed on the trunk, as a range string
  trunks[].active   VLANs allowed and active on it; the label falls back to
                    "allowed" when a platform doesn't report this

A label sits on the link, a short way out from the switch's end of it.
Segment nodes already carry their subnet's VLAN, so links to them aren't
labelled.
"""
from __future__ import annotations

import math

from features import Feature


def _along(points, distance):
    """The point `distance` along a polyline from its first point."""
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        seg = math.hypot(x2 - x1, y2 - y1)
        if seg >= distance and seg > 0:
            f = distance / seg
            return x1 + (x2 - x1) * f, y1 + (y2 - y1) * f
        distance -= seg
    return points[-1]


class VlansFeature(Feature):
    name = "vlans"
    label = "VLANs"
    facet = "vlans"
    SETTINGS = {
        "offset": 34.0,         # how far out along the link from the switch's node
        "max_chars": 24,        # longer trunk lists are cut short with "..."
        "style": "text;html=1;align=center;verticalAlign=middle;fontSize=8;"
                 "labelBackgroundColor=#FFF2CC;fontColor=#7F6000;spacing=1;",
    }

    def port_label(self, facet: dict, port: str) -> tuple[str, str] | None:
        """(label, tooltip) for one port, or None if the facet says nothing
        about it."""
        for t in facet.get("trunks", []):
            if t.get("port") == port:
                text = t.get("active") or t.get("allowed") or ""
                tip = f"{port}: trunk, native VLAN {t.get('native')}, VLANs {text or 'none'}"
                if len(text) > self.settings["max_chars"]:
                    text = text[:self.settings["max_chars"] - 3].rsplit(",", 1)[0] + ",..."
                return f"trunk {text}".strip(), tip
        for v in facet.get("vlans", []):
            if port in v.get("ports", []):
                return f"VLAN {v['id']}", f"{port}: access, VLAN {v['id']} ({v.get('name', '')})"
        return None

    def collect(self, topo):
        labels = []  # (edge id, end "source"/"target", text, tooltip)
        for e in topo.edges:
            if e.get("segment"):
                continue
            for end in ("source", "target"):
                host = e[end]
                facet = topo.facet(host, self.facet) if host in topo.hosts else None
                if not facet:
                    continue
                found = self.port_label(facet, e[f"{end}_intf"])
                if found:
                    labels.append((e["id"], end, *found))
        return labels

    def plan(self, labels, topo):
        placed = []
        for edge_id, end, text, tip in labels:
            spec = topo.specs.get(edge_id)
            if spec is None or not spec.segments:
                continue
            pts = [(spec.segments[0].p1.x, spec.segments[0].p1.y)]
            pts += [(s.p2.x, s.p2.y) for s in spec.segments]
            if end == "target":
                pts = pts[::-1]
            x, y = _along(pts, self.settings["offset"])
            placed.append((f"{edge_id}-{end}", x, y, text, tip))
        return placed

    def render(self, placed, layer):
        for local_id, x, y, text, tip in placed:
            layer.text(local_id, x, y, text, self.settings["style"],
                       w=max(40, 6 * len(text)), h=14, tooltip=tip)


FEATURE = VlansFeature()
