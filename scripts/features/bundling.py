"""
Geometry shared by overlay features (anything drawn as a path *across*
routers rather than between neighbours: IPsec today, GRE/MPLS LSPs later).

plan_overlays() takes overlays as host-name paths, e.g.
    {"t1": ["hq", "core1", "core2", "branch"], ...}
and returns, per overlay, the attachment points on its two end nodes plus
the waypoints a curved draw.io edge should pass through:

- Convergence at endpoints: every overlay terminating at the same node
  approaches it from one shared side, in evenly spaced parallel lanes that
  run straight for a stretch before meeting the node. Lanes are ordered by
  where each overlay is coming from, so they don't cross inside the bundle.
- Shared corridors: overlays that traverse the same physical hop run side
  by side in parallel lanes along it, then fan out where their paths split.
- Transit nodes: an overlay passes over each router it traverses (through
  the node, near its centre).

Lane order is chosen to keep lanes from crossing each other as they
converge and split: along corridors by which way an overlay turns at
either end of the hop, and at endpoints to match the first hop's lanes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

_SIDES = {  # outward unit normal of each node face
    "right": (1.0, 0.0), "left": (-1.0, 0.0),
    "bottom": (0.0, 1.0), "top": (0.0, -1.0),
}


@dataclass
class OverlayGeometry:
    id: str
    source: str
    target: str
    exit: tuple[float, float]       # perimeter fractions on source node
    entry: tuple[float, float]      # perimeter fractions on target node
    points: list[tuple[float, float]] = field(default_factory=list)


def _unit(dx, dy):
    length = math.hypot(dx, dy)
    return (dx / length, dy / length) if length > 1e-9 else (0.0, 0.0)


def _snap_side(dx, dy) -> str:
    if abs(dx) >= abs(dy):
        return "right" if dx >= 0 else "left"
    return "bottom" if dy >= 0 else "top"


def _lane_offsets(n: int, spacing: float) -> list[float]:
    return [(k - (n - 1) / 2) * spacing for k in range(n)]


def plan_overlays(paths: dict[str, list[str]], topo, spacing: float = 10.0,
                  converge_near: float = 30.0, converge_far: float = 90.0,
                  transit_spacing: float = 8.0) -> dict[str, OverlayGeometry]:
    paths = {k: v for k, v in paths.items() if len(v) >= 2}

    centroid = {}
    for oid, path in paths.items():
        pts = [topo.center(n) for n in path]
        centroid[oid] = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))

    # --- corridor lanes ---------------------------------------------------
    # (overlay, u, v) -> sideways offset while travelling the hop u-v,
    # measured to the left of the canonical direction u->v (u < v).
    # Lanes are ordered by how each overlay turns at either end of the hop
    # (screen coords, so a positive turn is clockwise / to the right): an
    # overlay that arrives from, or leaves towards, the right takes a
    # right-hand lane, so overlays don't cross as they join and leave.
    def turn(a, b, c):
        """Signed angle of the turn a->b->c, 0 if a or c is missing."""
        if a is None or c is None:
            return 0.0
        (ax, ay), (bx, by), (cx_, cy_) = topo.center(a), topo.center(b), topo.center(c)
        d1, d2 = _unit(bx - ax, by - ay), _unit(cx_ - bx, cy_ - by)
        return math.atan2(d1[0] * d2[1] - d1[1] * d2[0], d1[0] * d2[0] + d1[1] * d2[1])

    corridors: dict[tuple[str, str], list[tuple[float, str]]] = {}
    for oid, path in paths.items():
        for k, (a, b) in enumerate(zip(path, path[1:])):
            u, v = sorted((a, b))
            seq = path if a == u else path[::-1]   # walk the path in u->v order
            iu = seq.index(u)
            before = seq[iu - 1] if iu > 0 else None
            after = seq[iu + 2] if iu + 2 < len(seq) else None
            key = -(turn(before, u, v) + turn(u, v, after))
            corridors.setdefault((u, v), []).append((key, oid))
    corridor_offset = {}
    for (u, v), members in corridors.items():
        ordered = [oid for _, oid in sorted(members)]
        for oid, off in zip(ordered, _lane_offsets(len(ordered), transit_spacing)):
            corridor_offset[(oid, u, v)] = off

    def lane_vector(oid, a, b):
        """Sideways displacement of this overlay's lane on hop a-b."""
        u, v = sorted((a, b))
        (ux, uy), (vx, vy) = topo.center(u), topo.center(v)
        dx, dy = _unit(vx - ux, vy - uy)
        off = corridor_offset[(oid, u, v)]
        return dy * off, -dx * off

    def corridor_points(oid, a, b):
        """Two points along hop a->b (at 25% and 75%), in this overlay's lane."""
        ox, oy = lane_vector(oid, a, b)
        (ax, ay), (bx, by) = topo.center(a), topo.center(b)
        return [(ax + (bx - ax) * f + ox, ay + (by - ay) * f + oy) for f in (0.25, 0.75)]

    # --- endpoint bundles -------------------------------------------------
    # (overlay id, endpoint node) -> (face point, outward normal, offset index)
    terminals: dict[tuple[str, str], tuple[tuple[float, float], tuple[float, float], str]] = {}
    by_endpoint: dict[str, list[tuple[str, str]]] = {}  # node -> [(overlay, next hop)]
    for oid, path in paths.items():
        by_endpoint.setdefault(path[0], []).append((oid, path[1]))
        by_endpoint.setdefault(path[-1], []).append((oid, path[-2]))

    for node_name, members in by_endpoint.items():
        node = topo.nodes[node_name]
        cx, cy = topo.center(node_name)
        # Shared approach side: the mean direction towards the next hops.
        sx = sy = 0.0
        for _, nxt in members:
            ux, uy = _unit(topo.center(nxt)[0] - cx, topo.center(nxt)[1] - cy)
            sx, sy = sx + ux, sy + uy
        if abs(sx) < 1e-9 and abs(sy) < 1e-9:
            nx_, ny_ = topo.center(members[0][1])
            sx, sy = nx_ - cx, ny_ - cy
        side = _snap_side(sx, sy)
        normal = _SIDES[side]
        perp = (-normal[1], normal[0])

        # Order lanes along the face to match each overlay's lane on its
        # first hop (so the bundle doesn't twist), then by where its path
        # lies as a whole.
        def along(member):
            oid, nxt = member
            lx, ly = lane_vector(oid, node_name, nxt)
            px, py = centroid[oid]
            return (round(lx * perp[0] + ly * perp[1], 6),
                    (px - cx) * perp[0] + (py - cy) * perp[1], oid)
        members = sorted(members, key=along)

        face_len = node.h if side in ("left", "right") else node.w
        lane = spacing if len(members) < 2 else min(spacing, face_len * 0.8 / (len(members) - 1))
        half_extent = (node.w / 2, node.h / 2)
        for (oid, _), off in zip(members, _lane_offsets(len(members), lane)):
            fx = cx + normal[0] * half_extent[0] + perp[0] * off
            fy = cy + normal[1] * half_extent[1] + perp[1] * off
            terminals[(oid, node_name)] = ((fx, fy), normal, side)

    # --- assemble ---------------------------------------------------------
    def fractions(node_name, point):
        n = topo.nodes[node_name]
        return ((point[0] - n.x) / n.w, (point[1] - n.y) / n.h)

    def approach(point, normal):
        # Three collinear points: draw.io's curved style passes through the
        # midpoints between waypoints, so this keeps the lane straight
        # (and parallel to its neighbours) between near and far.
        mid = (converge_near + converge_far) / 2
        return [(point[0] + normal[0] * d, point[1] + normal[1] * d)
                for d in (converge_near, mid, converge_far)]

    result = {}
    for oid, path in paths.items():
        src, dst = path[0], path[-1]
        s_pt, s_norm, _ = terminals[(oid, src)]
        d_pt, d_norm, _ = terminals[(oid, dst)]

        points = approach(s_pt, s_norm)
        for i, (a, b) in enumerate(zip(path, path[1:])):
            hop_pts = corridor_points(oid, a, b)
            # The first/last hop's near-end point would fight the endpoint
            # bundle, which already covers that stretch.
            if i == 0:
                hop_pts = hop_pts[1:]
            if i == len(path) - 2:
                hop_pts = hop_pts[:-1]
            points.extend(hop_pts)
            if i < len(path) - 2:
                # Pass across the transit router, between its two lanes.
                (ix, iy), (jx, jy) = lane_vector(oid, a, b), lane_vector(oid, b, path[i + 2])
                bx, by = topo.center(b)
                points.append((bx + (ix + jx) / 2, by + (iy + jy) / 2))
        points.extend(reversed(approach(d_pt, d_norm)))

        result[oid] = OverlayGeometry(
            id=oid, source=src, target=dst,
            exit=fractions(src, s_pt), entry=fractions(dst, d_pt), points=points,
        )
    return result
