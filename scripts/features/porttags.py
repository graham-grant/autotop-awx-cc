"""
Shared base for features that tag link ends: a short label on a link, a
little way out from one device's end of it, saying something about that
device's port (its VLANs, its firewall zone).

A subclass sets name, label, facet and SETTINGS (with at least "offset" and
"style"), and implements port_label(facet, port) -> (text, tooltip) or
(text, tooltip, extra_style), or None for a port it has nothing to say
about. Links to segment nodes aren't tagged: the segment node already
names its network.
"""
from __future__ import annotations

import math

from features import Feature


def along(points, distance):
    """The point `distance` along a polyline from its first point."""
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        seg = math.hypot(x2 - x1, y2 - y1)
        if seg >= distance and seg > 0:
            f = distance / seg
            return x1 + (x2 - x1) * f, y1 + (y2 - y1) * f
        distance -= seg
    return points[-1]


class PortTagFeature(Feature):
    def port_label(self, facet: dict, port: str):
        raise NotImplementedError

    def collect(self, topo):
        labels = []  # (edge id, end "source"/"target", text, tooltip, extra style)
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
                    text, tip, *extra = found
                    labels.append((e["id"], end, text, tip, extra[0] if extra else ""))
        return labels

    def plan(self, labels, topo):
        placed = []
        for edge_id, end, text, tip, extra in labels:
            spec = topo.specs.get(edge_id)
            if spec is None or not spec.segments:
                continue
            pts = [(spec.segments[0].p1.x, spec.segments[0].p1.y)]
            pts += [(s.p2.x, s.p2.y) for s in spec.segments]
            if end == "target":
                pts = pts[::-1]
            x, y = along(pts, self.settings["offset"])
            placed.append((f"{edge_id}-{end}", x, y, text, tip, extra))
        return placed

    def render(self, placed, layer):
        for local_id, x, y, text, tip, extra in placed:
            layer.text(local_id, x, y, text, self.settings["style"] + extra,
                       w=max(40, 6 * len(text)), h=14, tooltip=tip)
