"""
Hanan-grid A* router for draw.io topology links.

This is the routing engine from drawio_hanan_router.py: grid lines are
built from every port/node-edge coordinate (the Hanan grid), then densified
with extra lines at `buffer` spacing so there's room for more than one lane
in open corridors. Node interiors are blocked vertices, with an exception
carved out only for the specific port a given link is using. A* runs over
(x_index, y_index, incoming_direction) so it can apply a turn penalty, and
treats the buffer rule as a hard edge filter (with a one-shot soft-penalty
fallback if no path exists otherwise). Links are routed one at a time,
"stamping" their path into a shared congestion map so later links see and
avoid earlier ones. Perpendicular crossings need no special handling here
at all - draw.io renders a jump wherever two jumpStyle-enabled edges
visually cross, so every emitted edge just gets jumpStyle set
unconditionally (that part lives in renderer.build_drawio_xml).

What this module does NOT decide on its own: which side of a node a given
link should attach to in the first place (top/bottom/left/right). That
choice is made by build_link_specs() below, using a per-node majority vote
over its own links (so a hub with links at slightly different angles still
ends up with all of them on one consistent side), with an optional
per-node override settable via a custom "side" property on a node in
template.drawio (see drawio_position_loader.py).
"""
from __future__ import annotations
import heapq
import math
from collections import defaultdict
from dataclasses import dataclass, field

from config import HANAN_BUFFER, HANAN_TURN_PENALTY, HANAN_MARGIN, HANAN_PERIMETER_MARGIN, HANAN_AFFINITY_PENALTY


def _load_c_extension():
    """The A* search runs in the required C extension csrc/_hanan.c.
    It's compiled on first use (and whenever the source is newer than the
    built module) by build_ext.py - stdlib only, but it does need a C
    compiler and Python.h on this machine."""
    import build_ext
    try:
        if build_ext.is_stale():
            build_ext.build()
        import _hanan
    except (build_ext.BuildError, ImportError) as e:
        raise RuntimeError(
            f"The required C extension _hanan couldn't be built or loaded: {e}\n"
            f"Run `python3 scripts/build_ext.py` to see the full error."
        ) from e
    return _hanan


_hanan = _load_c_extension()


# ---------------------------------------------------------------------------
# Geometry primitives
# ---------------------------------------------------------------------------

def _key(v: float) -> float:
    """Rounding used for coordinate equality / dict lookups."""
    return round(v, 3)


@dataclass(frozen=True)
class Point:
    x: float
    y: float


@dataclass
class Node:
    id: str
    x: float
    y: float
    w: float
    h: float

    def port(self, side: str, frac: float) -> Point:
        if side == "top":
            return Point(self.x + frac * self.w, self.y)
        if side == "bottom":
            return Point(self.x + frac * self.w, self.y + self.h)
        if side == "left":
            return Point(self.x, self.y + frac * self.h)
        if side == "right":
            return Point(self.x + self.w, self.y + frac * self.h)
        raise ValueError(side)


@dataclass
class Segment:
    p1: Point
    p2: Point
    link_id: str = ""
    target_id: str = "" # Add this new property

    @property
    def orientation(self) -> str:
        return "H" if abs(self.p1.y - self.p2.y) < 1e-6 else "V"

    @property
    def fixed_coord(self) -> float:
        return self.p1.y if self.orientation == "H" else self.p1.x

    @property
    def span(self) -> tuple[float, float]:
        if self.orientation == "H":
            return (min(self.p1.x, self.p2.x), max(self.p1.x, self.p2.x))
        return (min(self.p1.y, self.p2.y), max(self.p1.y, self.p2.y))


def _ranges_overlap(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def parallel_conflict(a: Segment, b: Segment, buffer: float) -> bool:
    """True if a and b run parallel and sit closer than `buffer` apart
    over an overlapping stretch -- the hard constraint from the spec.
    A tiny epsilon keeps a pair that's meant to sit exactly on the buffer
    boundary from tripping this purely due to float rounding."""
    if a.orientation != b.orientation:
        return False
    if abs(a.fixed_coord - b.fixed_coord) >= buffer - 1e-6:
        return False
    return _ranges_overlap(a.span, b.span)


@dataclass
class LinkSpec:
    id: str
    src: Node
    dst: Node
    src_side: str
    dst_side: str
    src_frac: float | None = None
    dst_frac: float | None = None
    segments: list[Segment] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Stage 1: port assignment
# ---------------------------------------------------------------------------

def _get_valid_ports(grid: HananGrid, node: Node, side: str, used_ports: set[Point], buffer: float, force_center: bool = False) -> list[Point]:
    """Returns all grid points on the specified node face that are at least
    `buffer` distance away from any already-claimed port on this node."""
    pts = []
    if side == "top":
        for x in grid.xs:
            if node.x <= x <= node.x + node.w:
                pts.append(Point(x, node.y))
    elif side == "bottom":
        for x in grid.xs:
            if node.x <= x <= node.x + node.w:
                pts.append(Point(x, node.y + node.h))
    elif side == "left":
        for y in grid.ys:
            if node.y <= y <= node.y + node.h:
                pts.append(Point(node.x, y))
    elif side == "right":
        for y in grid.ys:
            if node.y <= y <= node.y + node.h:
                pts.append(Point(node.x + node.w, y))

    valid = []
    for p in pts:
        # Reject points that violate the buffer distance of an established port
        conflict = False
        for u in used_ports:
            if abs(p.x - u.x) < buffer - 1e-6 and abs(p.y - u.y) < buffer - 1e-6:
                conflict = True
                break
        if not conflict:
            valid.append(p)

    # If this is the only link on this face, restrict it to the exact center
    if force_center and valid:
        if side in ("top", "bottom"):
            center = node.x + node.w / 2.0
            valid.sort(key=lambda p: abs(p.x - center))
        else:
            center = node.y + node.h / 2.0
            valid.sort(key=lambda p: abs(p.y - center))
        return [valid[0]]

    return valid


def _group_by_side(specs: list[LinkSpec]) -> dict[tuple[str, str], list[LinkSpec]]:
    groups: dict[tuple[str, str], list[LinkSpec]] = {}
    for s in specs:
        groups.setdefault((s.src.id, s.src_side), []).append(s)
        groups.setdefault((s.dst.id, s.dst_side), []).append(s)
    return groups


# ---------------------------------------------------------------------------
# Stage 1b: node capacity check -- grow a side before it gets overcrowded
# ---------------------------------------------------------------------------

def _min_side_length(n: int, buffer: float, margin: float) -> float:
    """Side length needed so n ports, spaced the way assign_ports_for_side
    spaces them, clear `buffer` between every adjacent pair. A single port
    has no neighbour to keep clear of, so it imposes no minimum.

    Padded by a hair over the exact algebraic minimum: computing this at
    the precise boundary means the resulting port spacing lands exactly on
    `buffer` only in theory -- in practice, the chain of float operations
    between here and the actual port coordinates (centering the resize,
    then assign_ports_for_side's own fraction math) accumulates rounding
    error in either direction. Landing a hair under the boundary is enough
    to make the hard buffer check reject a lane that was meant to just
    clear it, which cascades into routing failures on larger fan-outs.
    """
    if n <= 1:
        return 0.0
    exact = buffer * (n - 1) / (1 - 2 * margin)
    return exact * 1.001 + 1e-6


def ensure_node_capacity(
    nodes: list[Node],
    groups: dict[tuple[str, str], list[LinkSpec]],
    buffer: float,
    margin: float = 0.12,
) -> None:
    """Grow any node whose side doesn't have room for all the links
    assigned to it at >= buffer spacing -- this is what was causing A* to
    fail outright: with ports already closer than `buffer` at the node
    itself, the very first edges leaving it violate the hard buffer
    constraint with nowhere else to go. Resizing happens around the node's
    center so its layout position is otherwise undisturbed.
    """
    for node in nodes:
        n_top = len(groups.get((node.id, "top"), []))
        n_bottom = len(groups.get((node.id, "bottom"), []))
        n_left = len(groups.get((node.id, "left"), []))
        n_right = len(groups.get((node.id, "right"), []))

        required_w = max(
            _min_side_length(n_top, buffer, margin),
            _min_side_length(n_bottom, buffer, margin),
            node.w,
        )
        required_h = max(
            _min_side_length(n_left, buffer, margin),
            _min_side_length(n_right, buffer, margin),
            node.h,
        )

        if required_w > node.w:
            node.x -= (required_w - node.w) / 2
            node.w = required_w
        if required_h > node.h:
            node.y -= (required_h - node.h) / 2
            node.h = required_h


# ---------------------------------------------------------------------------
# Stage 2: Hanan grid (densified for lane room) + obstacle blocking
# ---------------------------------------------------------------------------

def _densify(coords: list[float], pitch: float) -> list[float]:
    """Insert extra grid lines at `pitch` spacing inside any gap wider than
    `pitch`, so open corridors have room for more than one lane."""
    if not coords:
        return coords
    out = [coords[0]]
    for a, b in zip(coords, coords[1:]):
        gap = b - a
        if gap > pitch:
            steps = int(gap // pitch)
            for k in range(1, steps + 1):
                v = a + k * pitch
                if v < b - 1e-6:
                    out.append(v)
        out.append(b)
    return out


_SOFT_PENALTY = 5000.0  # fallback cost when no path respects the buffer at all
_PERIMETER_PENALTY = 500.0  # Skin-tight perimeter penalty to stop face scraping
_RUNWAY_PENALTY = 400.0     # Blocks paths from cruising down the face-aligned grid lines
_CORNER_PENALTY = 250.0     # Outer diagonal corner penalty


class HananGrid:
    def __init__(self, nodes: list[Node], terminals: list[Point], pitch: float, perimeter_margin: float = 0.0):
        self.nodes = nodes

        # Inject the exact center of each node to guarantee a perfect center runway
        xs_raw = {n.x for n in nodes} | {n.x + n.w for n in nodes} | {n.x + n.w / 2.0 for n in nodes} | {p.x for p in terminals}
        ys_raw = {n.y for n in nodes} | {n.y + n.h for n in nodes} | {n.y + n.h / 2.0 for n in nodes} | {p.y for p in terminals}

        if perimeter_margin > 0:
            xs_raw |= {min(xs_raw) - perimeter_margin, max(xs_raw) + perimeter_margin}
            ys_raw |= {min(ys_raw) - perimeter_margin, max(ys_raw) + perimeter_margin}

        self.xs = _densify(sorted(xs_raw), pitch)
        self.ys = _densify(sorted(ys_raw), pitch)
        self.xi = {_key(v): i for i, v in enumerate(self.xs)}
        self.yi = {_key(v): i for i, v in enumerate(self.ys)}
        self.base_blocked = self._compute_base_blocked()

        # Compute static penalties (Moat + Runway Extensions + Corners)
        self.base_penalty = self._compute_base_penalty(pitch, pitch * 1.5)

    def _compute_base_blocked(self) -> list[list[bool]]:
        blocked = [[False] * len(self.ys) for _ in self.xs]
        for ix, x in enumerate(self.xs):
            for iy, y in enumerate(self.ys):
                for n in self.nodes:
                    if n.x <= x <= n.x + n.w and n.y <= y <= n.y + n.h:
                        blocked[ix][iy] = True
                        break
        return blocked

    def _compute_base_penalty(self, pitch: float, zone_size: float) -> list[list[float]]:
        penalties = [[0.0] * len(self.ys) for _ in self.xs]
        for ix, x in enumerate(self.xs):
            for iy, y in enumerate(self.ys):
                penalty = 0.0

                for n in self.nodes:
                    dx = max(0.0, n.x - x, x - (n.x + n.w))
                    dy = max(0.0, n.y - y, y - (n.y + n.h))

                    # 1. Skin-Tight Perimeter Moat (stops lateral face scraping)
                    if dx < 1e-4 and dy < 1e-4:
                        penalty += _PERIMETER_PENALTY
                        continue

                    # 2. Face Runway Extensions (blocks the grid lines expanding out from faces)
                    is_collinear_h = (abs(y - n.y) < 1e-4) or (abs(y - (n.y + n.h)) < 1e-4)
                    is_collinear_v = (abs(x - n.x) < 1e-4) or (abs(x - (n.x + n.w)) < 1e-4)

                    if (is_collinear_h and dx > 0.0 and dx <= zone_size) or \
                       (is_collinear_v and dy > 0.0 and dy <= zone_size):
                        penalty += _RUNWAY_PENALTY
                        continue

                    # 3. Outer Diagonal Corners
                    elif 0 < dx <= zone_size and 0 < dy <= zone_size:
                        in_clear_lane = False
                        for other_n in self.nodes:
                            if other_n is n:
                                continue
                            odx = max(0.0, other_n.x - x, x - (other_n.x + other_n.w))
                            ody = max(0.0, other_n.y - y, y - (other_n.y + other_n.h))

                            if (odx == 0.0 and ody <= max(zone_size, other_n.h)) or \
                               (ody == 0.0 and odx <= max(zone_size, other_n.w)):
                                in_clear_lane = True
                                break

                        if not in_clear_lane:
                            penalty += _CORNER_PENALTY

                penalties[ix][iy] = penalty

        return penalties

        penalties = [[0.0] * len(self.ys) for _ in self.xs]
        for ix, x in enumerate(self.xs):
            for iy, y in enumerate(self.ys):
                # 1. Determine if this point is in the direct orthogonal path of ANY node,
                # bounding the search radius to the node's own dimensions (or zone_size)
                # so an infinite "clear lane" doesn't wipe out corner penalties globally.
                in_clear_lane = False
                for n in self.nodes:
                    dx = max(0.0, n.x - x, x - (n.x + n.w))
                    dy = max(0.0, n.y - y, y - (n.y + n.h))
                    
                    # Only project the clear lane outward by the node's width/height
                    if (dx == 0.0 and dy <= max(zone_size, n.h)) or \
                       (dy == 0.0 and dx <= max(zone_size, n.w)):
                        in_clear_lane = True
                        break
                
                # If it falls in a local clear linking lane, it supersedes corner penalties
                if in_clear_lane:
                    continue
                
                # 2. Otherwise, accumulate penalties for proximity to diagonal corners
                penalty = 0.0
                for n in self.nodes:
                    dx = max(0.0, n.x - x, x - (n.x + n.w))
                    dy = max(0.0, n.y - y, y - (n.y + n.h))
                    
                    if 0 < dx <= zone_size and 0 < dy <= zone_size:
                        penalty += _CORNER_PENALTY
                        
                penalties[ix][iy] = penalty
                
        return penalties

    def index_of(self, p: Point) -> tuple[int, int]:
        return self.xi[_key(p.x)], self.yi[_key(p.y)]

    def coord_of(self, i: int, j: int) -> Point:
        return Point(self.xs[i], self.ys[j])

    def neighbors(self, i: int, j: int):
        for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ni, nj = i + di, j + dj
            if 0 <= ni < len(self.xs) and 0 <= nj < len(self.ys):
                yield ni, nj

    def blocked(self, i: int, j: int, allowed: set[Point]) -> bool:
        """A point is impassable if it's inside some node's rectangle --
        unless it's exactly one of this link's own two terminals."""
        if not self.base_blocked[i][j]:
            return False
        return self.coord_of(i, j) not in allowed


# ---------------------------------------------------------------------------
# Stage 3: congestion map -- the live version of the buffer/lane check
# ---------------------------------------------------------------------------

class CongestionMap:
    def __init__(self):
        self.segments: list[Segment] = []

    def conflicts(self, candidate: Segment, buffer: float) -> bool:
        return any(parallel_conflict(candidate, s, buffer) for s in self.segments)

    def is_adjacent(self, candidate: Segment, target_id: str, buffer: float) -> bool:
        """True if the candidate runs parallel, exactly `buffer` away, AND shares the same target."""
        return any(
            s.target_id == target_id and
            candidate.orientation == s.orientation and
            abs(abs(candidate.fixed_coord - s.fixed_coord) - buffer) < 1e-4 and
            _ranges_overlap(candidate.span, s.span)
            for s in self.segments
        )

    def add_path(self, link_id: str, target_id: str, points: list[Point]) -> list[Segment]:
        segs = _collapse_to_segments(link_id, target_id, points)
        self.segments.extend(segs)
        return segs

def _collapse_to_segments(link_id: str, target_id: str, points: list[Point]) -> list[Segment]:
    """Merge consecutive grid steps in the same direction into one segment."""
    if len(points) < 2:
        return []
    segs: list[Segment] = []
    start = points[0]
    prev_dir = None
    for p_prev, p_next in zip(points, points[1:]):
        cur_dir = "H" if p_prev.y == p_next.y else "V"
        if prev_dir is not None and cur_dir != prev_dir:
            segs.append(Segment(start, p_prev, link_id, target_id))
            start = p_prev
        prev_dir = cur_dir
    segs.append(Segment(start, points[-1], link_id, target_id))
    return segs


# ---------------------------------------------------------------------------
# Stage 4: A* with direction-aware turn penalty + hard/soft buffer modes
# ---------------------------------------------------------------------------

_SOFT_PENALTY = 5000.0  # fallback cost when no path respects the buffer at all


def _flat_grid(grid: HananGrid):
    """blocked/penalty flattened (index i*len(ys)+j) for the C search;
    computed once per grid, since both are static."""
    cached = getattr(grid, "_flat", None)
    if cached is None:
        blocked = bytes(1 if b else 0 for col in grid.base_blocked for b in col)
        penalty = [p for col in grid.base_penalty for p in col]
        cached = grid._flat = (blocked, penalty)
    return cached


def astar_route(
    grid: HananGrid,
    congestion: CongestionMap,
    start_pts: list[Point],
    goal_pts: list[Point],
    target_id: str,
    buffer: float,
    turn_penalty: float,
    affinity_penalty: float,
    hard_buffer: bool = True,
) -> list[Point] | None:
    """A* over the Hanan grid, in C (csrc/_hanan.c). Same inputs, costs
    and result as astar_route_py below, which is kept as the readable
    reference implementation and checked against this in the tests."""
    blocked, penalty = _flat_grid(grid)
    segs = [(s.orientation == "H", s.fixed_coord, *s.span, s.target_id == target_id)
            for s in congestion.segments]
    path = _hanan.astar(
        grid.xs, grid.ys, blocked, penalty, segs,
        [grid.index_of(p) for p in start_pts], [grid.index_of(p) for p in goal_pts],
        buffer, turn_penalty, affinity_penalty, _SOFT_PENALTY, hard_buffer,
    )
    return None if path is None else [grid.coord_of(i, j) for i, j in path]


def astar_route_py(
    grid: HananGrid,
    congestion: CongestionMap,
    start_pts: list[Point],
    goal_pts: list[Point],
    target_id: str,
    buffer: float,
    turn_penalty: float,
    affinity_penalty: float,  # Adjust this: higher means more aggressive clustering
    hard_buffer: bool = True,
) -> list[Point] | None:
    
    goal_indices = {grid.index_of(p) for p in goal_pts}
    allowed = set(goal_pts)

    def h(i: int, j: int) -> float:
        x, y = grid.xs[i], grid.ys[j]
        return min(abs(x - gp.x) + abs(y - gp.y) for gp in goal_pts)

    open_heap = []
    best_g = {}
    came_from = {}

    for p in start_pts:
        si, sj = grid.index_of(p)
        state = (si, sj, None)
        best_g[state] = 0.0
        heapq.heappush(open_heap, (h(si, sj), 0.0, state))

    while open_heap:
        _, g, state = heapq.heappop(open_heap)
        i, j, d = state
        
        if best_g.get(state, math.inf) < g:
            continue
            
        if (i, j) in goal_indices:
            return _reconstruct(came_from, state, grid)

        for ni, nj in grid.neighbors(i, j):
            if grid.blocked(ni, nj, allowed):
                continue
                
            new_dir = "H" if ni != i else "V"
            here = grid.coord_of(i, j)
            there = grid.coord_of(ni, nj)
            
            step_dist = abs(there.x - here.x) + abs(there.y - here.y)
            step_cost = step_dist
            
            if d is not None and new_dir != d:
                step_cost += turn_penalty
                
            step_cost += grid.base_penalty[ni][nj]
            
            cand_segment = Segment(here, there)
            
            if congestion.conflicts(cand_segment, buffer):
                if hard_buffer:
                    continue
                step_cost += _SOFT_PENALTY
            elif congestion.segments:
                # If there are existing lanes going to the same target, penalize paths that don't hug them
                if not congestion.is_adjacent(cand_segment, target_id, buffer):
                    step_cost += step_dist * affinity_penalty

            new_state = (ni, nj, new_dir)
            tentative_g = g + step_cost
            if tentative_g < best_g.get(new_state, math.inf):
                best_g[new_state] = tentative_g
                came_from[new_state] = state
                heapq.heappush(open_heap, (tentative_g + h(ni, nj), tentative_g, new_state))

    return None


def _reconstruct(came_from: dict, state: tuple, grid: HananGrid) -> list[Point]:
    states = [state]
    while state in came_from:
        state = came_from[state]
        states.append(state)
    states.reverse()
    return [grid.coord_of(i, j) for (i, j, _) in states]


# ---------------------------------------------------------------------------
# Stage 5: orchestration
# ---------------------------------------------------------------------------

def route_topology(
    nodes: list[Node],
    specs: list[LinkSpec],
    buffer: float = 20.0,
    turn_penalty: float = 15.0,
    margin: float = 0.12,
    affinity_penalty: float = 1.5, # Expose the parameter here
    perimeter_margin: float | None = None,
) -> None:
    groups = _group_by_side(specs)

    # Grow nodes to ensure their perimeter can fit all required links
    ensure_node_capacity(nodes, groups, buffer, margin)

    if perimeter_margin is None:
        max_group = max((len(g) for g in groups.values()), default=1)
        perimeter_margin = buffer * (max_group + 2)

    # Initialize grid without forced terminals; _densify provides the port grid lines
    grid = HananGrid(nodes, [], pitch=buffer, perimeter_margin=perimeter_margin)
    congestion = CongestionMap()
    used_ports = set()

    link_counter = 0
    num_links = len(specs)
    print("Generating links...")
    for s in specs:
        link_counter = link_counter + 1
        print(f"\rProgress: {link_counter}/{num_links}", end="", flush=True)

        start_pts = _get_valid_ports(grid, s.src, s.src_side, used_ports, buffer)
        goal_pts = _get_valid_ports(grid, s.dst, s.dst_side, used_ports, buffer)

        if not start_pts or not goal_pts:
            raise RuntimeError(f"Ran out of valid capacity for link {s.id}")

        # Pass affinity_penalty into the A* calls
        path = astar_route(grid, congestion, start_pts, goal_pts, s.dst.id, buffer, turn_penalty, affinity_penalty, hard_buffer=True)
        if path is None:
            path = astar_route(grid, congestion, start_pts, goal_pts, s.dst.id, buffer, turn_penalty, affinity_penalty, hard_buffer=False)
        if path is None:
            raise RuntimeError(f"no route found for link {s.id}")

        s.segments = congestion.add_path(s.id, s.dst.id, path)

        # Record chosen ports so later links route around them
        src_pt, dst_pt = path[0], path[-1]
        used_ports.add(src_pt)
        used_ports.add(dst_pt)

        # Reverse-calculate fractions for the renderer's exitX/exitY attributes
        if s.src_side in ("top", "bottom"):
            s.src_frac = (src_pt.x - s.src.x) / s.src.w
        else:
            s.src_frac = (src_pt.y - s.src.y) / s.src.h

        if s.dst_side in ("top", "bottom"):
            s.dst_frac = (dst_pt.x - s.dst.x) / s.dst.w
        else:
            s.dst_frac = (dst_pt.y - s.dst.y) / s.dst.h


# ---------------------------------------------------------------------------
# Side selection: decide which side of each node a link attaches to. The
# Hanan router above takes src_side/dst_side as given; it doesn't choose
# them. This is the one piece kept from the project's earlier heuristic
# routing approach, since the Hanan router has no opinion on it either.
# ---------------------------------------------------------------------------



def choose_sides(edges, centers, preferred_sides=None, layer_tolerance=40.0):
    """For every edge, decide which side of each endpoint it attaches to.
    Returns {edge_id: (source_side, target_side)}.
    
    Implements a top-down Sugiyama-style heuristic: 
    - Nodes on the same vertical layer (within layer_tolerance) connect Left-to-Right.
    - Nodes on different layers connect Top-to-Bottom.
    """
    preferred_sides = preferred_sides or {}
    edge_sides = {}

    for e in edges:
        src = e["source"]
        dst = e["target"]

        # 1. Fetch pinned manual overrides first
        s_side = preferred_sides.get(src)
        t_side = preferred_sides.get(dst)

        # 2. If sides aren't pinned, calculate based on relative geometry
        if not s_side or not t_side:
            cx_s, cy_s = centers[src]
            cx_t, cy_t = centers[dst]

            # If the difference in Y is minor, they belong to the same Sugiyama layer
            same_layer = abs(cy_s - cy_t) <= layer_tolerance

            if not s_side:
                if same_layer:
                    # Source is to the left of Target -> use right side
                    s_side = "right" if cx_s < cx_t else "left"
                else:
                    # Source is above Target -> use bottom side
                    s_side = "bottom" if cy_s < cy_t else "top"

            if not t_side:
                if same_layer:
                    # Target is to the right of Source -> use left side
                    t_side = "left" if cx_s < cx_t else "right"
                else:
                    # Target is below Source -> use top side
                    t_side = "top" if cy_s < cy_t else "bottom"

        edge_sides[e["id"]] = (s_side, t_side)
        
    return edge_sides
    """For every edge, decide which side of each endpoint it attaches to.
    Returns {edge_id: (source_side, target_side)}.
    """
    preferred_sides = preferred_sides or {}
    node_edges = defaultdict(list)
    for e in edges:
        node_edges[e["source"]].append(e)
        node_edges[e["target"]].append(e)
    node_axis = {
        n: _node_dominant_axis(n, es, centers)
        for n, es in node_edges.items() if n not in preferred_sides
    }

    edge_sides = {}
    for e in edges:
        s_side = preferred_sides.get(e["source"]) or \
            _compute_side(e["source"], e["target"], node_axis[e["source"]], centers)
        t_side = preferred_sides.get(e["target"]) or \
            _compute_side(e["target"], e["source"], node_axis[e["target"]], centers)
        edge_sides[e["id"]] = (s_side, t_side)
    return edge_sides


def build_link_specs(edges, centers, sizes, preferred_sides=None):
    """Convert this project's plain edge dicts + node positions/sizes into
    the Hanan router's Node/LinkSpec objects, choose sides, then run
    route_topology(). Returns (hanan_nodes, specs) ready for rendering.
    """
    edge_sides = choose_sides(edges, centers, preferred_sides)

    hanan_nodes = {
        name: Node(id=name, x=cx - sizes[name][0] / 2, y=cy - sizes[name][1] / 2,
                   w=sizes[name][0], h=sizes[name][1])
        for name, (cx, cy) in centers.items()
    }

    specs = []
    for e in edges:
        s_side, t_side = edge_sides[e["id"]]
        specs.append(LinkSpec(
            id=e["id"], src=hanan_nodes[e["source"]], dst=hanan_nodes[e["target"]],
            src_side=s_side, dst_side=t_side,
        ))

    route_topology(
        list(hanan_nodes.values()), specs,
        buffer=HANAN_BUFFER, turn_penalty=HANAN_TURN_PENALTY,
        affinity_penalty=HANAN_AFFINITY_PENALTY,
        margin=HANAN_MARGIN, perimeter_margin=HANAN_PERIMETER_MARGIN,
    )
    return list(hanan_nodes.values()), specs
