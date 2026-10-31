"""
The diagram, independent of any file format.

renderer.py and the feature plugins (features/) draw into a Diagram; the
writers turn it into files: drawio.py writes draw.io, vsdx.py writes
Microsoft Visio. Neither writer reads the other's output, so either format
can be dropped without touching the other, and a feature draws once for
both.

  Diagram       pages
  Page          id, name, layers, and its items in drawing order
  Layer         a named, toggleable layer (feature data). Items with no
                layer are on the base layer
  Shape         a box, ellipse or free text at x, y (top-left), w x h,
                optionally showing an Icon
  Line          a connection between two shapes: its waypoints, and where
                it leaves the source and meets the target (perimeter
                fractions) - Page.route() turns that into the full polyline
  LineLabel     a label riding on a Line (interface names): where along it
                (-1 = source end, 0 = middle, 1 = target end) and an offset

Coordinates are in pixels at 96 per inch, y pointing down.

Every item can carry a tooltip, a link (to another page: "page:<id>"; a
web address; or "page:<id>#highlight=<tag>,<tag>" - the page to open and
the tagged items to highlight there, which writers that can't highlight
just open), tags, and free data (key -> text), which the writers turn
into each format's own equivalent.

A style is a plain dict of these keys, all optional:

  kind          "box" (default), "ellipse" or "text" (no box drawn unless a
                fill or stroke is given)
  fill          fill colour ("#RRGGBB"); for a box, white when unset
  stroke        outline / line colour; for a box or line, black when unset;
                "none" for no outline
  stroke_width  in pixels (1)
  dashed        True for a dashed outline or line; dash = "8 4" sets the
                pattern
  rounded       True for rounded corners; arc = radius as a percent of the
                shorter side (15)
  font_size     in pixels (12); font_color; bold
  align         "left", "center" (default) or "right"; valign "top",
                "middle" (default) or "bottom"
  rotation      degrees, clockwise
  label_bg      a text item's background colour; label_border its outline
  spacing       padding around text, in pixels
  wrap          True to wrap text to the shape's width
  curved        a Line drawn as a smooth curve through its waypoints
  start_arrow,  "none", "classic" (default at the end), "block", "open",
  end_arrow     "oval", "diamond"
  jumps         a Line hops over the lines it crosses (draw.io only)
"""
from __future__ import annotations

import base64
import math
import re
import struct
from dataclasses import dataclass, field

STYLE_KEYS = {
    "kind", "fill", "stroke", "stroke_width", "dashed", "dash", "rounded", "arc",
    "font_size", "font_color", "bold", "align", "valign", "rotation", "label_bg",
    "label_border", "spacing", "wrap", "curved", "start_arrow", "end_arrow", "jumps",
}
DEFAULT_FONT_SIZE = 12.0
DEFAULT_ARC = 15.0


def style(**kw) -> dict:
    """A style dict, checking the keys so a typo fails loudly."""
    check_style(kw)
    return kw


def check_style(st: dict) -> dict:
    unknown = set(st) - STYLE_KEYS
    if unknown:
        raise ValueError(f"unknown style key(s): {', '.join(sorted(unknown))}")
    return st


# ---------------------------------------------------------------------------
# Icons
# ---------------------------------------------------------------------------

def png_size(data: bytes) -> tuple[int, int] | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    return None


@dataclass
class Icon:
    """A picture shown in place of a node's box, letterboxed to its own
    aspect ratio. data is a PNG or an SVG (mime "image/png" or
    "image/svg+xml"); label_below puts the node's name under it."""
    data: bytes
    mime: str = "image/png"
    label_below: bool = True

    def png(self) -> bytes | None:
        """The picture as a PNG: the data itself, or the one PNG an SVG
        does nothing but wrap (as the icon library's SVGs do). None for any
        other SVG."""
        if self.mime == "image/png":
            return self.data if png_size(self.data) else None
        svg = self.data.decode("utf-8", "replace")
        found = re.findall(r'<image\b[^>]*href="data:image/png;base64,([^"]+)"', svg)
        drawn = set(re.findall(r"<(\w+)", svg)) - {"svg", "image", "defs", "g", "title", "desc"}
        if len(found) == 1 and not drawn:
            png = base64.b64decode(found[0])
            return png if png_size(png) else None
        return None


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

@dataclass
class Layer:
    id: str
    name: str
    visible: bool = True
    locked: bool = False


@dataclass
class Item:
    id: str
    style: dict = field(default_factory=dict)
    text: str = ""
    layer: str | None = None        # Layer.id; None = the base layer
    tooltip: str | None = None
    link: str | None = None
    tags: tuple = ()
    data: dict = field(default_factory=dict)

    def __post_init__(self):
        check_style(self.style)


@dataclass
class Shape(Item):
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    icon: Icon | None = None

    @property
    def rect(self):
        return self.x, self.y, self.w, self.h


@dataclass
class Line(Item):
    source: str = ""
    target: str = ""
    waypoints: list = field(default_factory=list)   # [(x, y), ...] between the ends
    exit: tuple | None = None       # (fx, fy): where it leaves the source, as fractions
    entry: tuple | None = None      # of the shape's width and height (0,0 = top-left)


@dataclass
class LineLabel(Item):
    line: str = ""                  # Line.id
    at: float = 0.0                 # -1 source end ... 0 middle ... 1 target end
    offset: tuple = (0.0, 0.0)
    w: float = 0.0                  # 0 = sized to the text
    h: float = 0.0


@dataclass
class Page:
    id: str
    name: str
    layers: list = field(default_factory=list)
    items: list = field(default_factory=list)

    def __post_init__(self):
        self._ids = {i.id for i in self.items}

    def add(self, item: Item) -> Item:
        if item.id in self._ids:
            raise ValueError(f"duplicate item id {item.id}")
        self._ids.add(item.id)
        self.items.append(item)
        return item

    def add_layer(self, layer: Layer) -> Layer:
        self.layers.append(layer)
        return layer

    def has(self, item_id: str) -> bool:
        return item_id in self._ids

    def get(self, item_id: str) -> Item | None:
        return next((i for i in self.items if i.id == item_id), None)

    def route(self, line: Line) -> list:
        """The full polyline of a Line: its exit point on the source shape,
        its waypoints, and its entry point on the target. An end without an
        exit/entry fraction is where the line from the shape's centre
        toward the next point crosses its border."""
        src, dst = self.get(line.source), self.get(line.target)
        if not isinstance(src, Shape) or not isinstance(dst, Shape):
            return []
        pts = [tuple(p) for p in line.waypoints]
        start = _end(src.rect, line.exit, pts[0] if pts else _centre(dst.rect))
        finish = _end(dst.rect, line.entry, pts[-1] if pts else _centre(src.rect))
        full = [start] + pts + [finish]
        return curve(full) if line.style.get("curved") else full

    def label_point(self, label: LineLabel):
        """Where a LineLabel's anchor sits: `at` along its line, plus offset."""
        line = self.get(label.line)
        pts = self.route(line) if isinstance(line, Line) else []
        if not pts:
            return None
        x, y = along(pts, (label.at + 1) / 2)
        return x + label.offset[0], y + label.offset[1]


@dataclass
class Diagram:
    pages: list = field(default_factory=list)

    def page(self, page_id: str) -> Page | None:
        return next((p for p in self.pages if p.id == page_id), None)


# ---------------------------------------------------------------------------
# Geometry shared by the writers
# ---------------------------------------------------------------------------

def _centre(rect):
    x, y, w, h = rect
    return x + w / 2, y + h / 2


def _end(rect, frac, toward):
    x, y, w, h = rect
    if frac is not None:
        return x + float(frac[0]) * w, y + float(frac[1]) * h
    return clip(rect, toward)


def clip(rect, toward):
    """Where the line from rect's centre toward a point leaves the rect."""
    x, y, w, h = rect
    cx, cy = x + w / 2, y + h / 2
    dx, dy = toward[0] - cx, toward[1] - cy
    if (dx == 0 and dy == 0) or w == 0 or h == 0:
        return cx, cy
    t = min(abs((w / 2) / dx) if dx else math.inf, abs((h / 2) / dy) if dy else math.inf)
    t = min(t, 1.0)
    return cx + dx * t, cy + dy * t


def along(points, fraction):
    """The point `fraction` (0..1) of the way along a polyline."""
    lengths = [math.dist(a, b) for a, b in zip(points, points[1:])]
    total = sum(lengths)
    if total == 0:
        return points[0]
    want = max(0.0, min(1.0, fraction)) * total
    for (a, b), seg in zip(zip(points, points[1:]), lengths):
        if want <= seg and seg > 0:
            f = want / seg
            return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
        want -= seg
    return points[-1]


def curve(points, steps=8):
    """A smooth curve as a polyline: quadratic segments through the
    midpoints of the waypoints, each waypoint a control point (the same
    curve draw.io draws for curved=1)."""
    if len(points) < 3:
        return list(points)

    def quad(a, c, b):
        return [((1 - t) ** 2 * a[0] + 2 * (1 - t) * t * c[0] + t * t * b[0],
                 (1 - t) ** 2 * a[1] + 2 * (1 - t) * t * c[1] + t * t * b[1])
                for t in (i / steps for i in range(1, steps + 1))]

    out, start = [points[0]], points[0]
    for i in range(1, len(points) - 2):
        mid = ((points[i][0] + points[i + 1][0]) / 2, (points[i][1] + points[i + 1][1]) / 2)
        out += quad(start, points[i], mid)
        start = mid
    out += quad(start, points[-2], points[-1])
    return out


def text_size(text: str, font_px: float) -> tuple[float, float]:
    """An estimate of a text's size, for labels sized to their text."""
    lines = text.split("\n") if text else [""]
    width = max(len(line) for line in lines) * font_px * 0.6 + 6
    return max(width, 8.0), len(lines) * font_px * 1.25 + 4


def parse_link(link: str | None):
    """("page", page id, [tags to highlight]) | ("url", address, []) | None."""
    if not link:
        return None
    if link.startswith("page:"):
        target, _, extra = link[5:].partition("#")
        tags = extra[len("highlight="):].split(",") if extra.startswith("highlight=") else []
        return "page", target, [t for t in tags if t]
    if re.match(r"https?://", link):
        return "url", link, []
    return None
