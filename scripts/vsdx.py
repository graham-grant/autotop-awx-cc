"""
Visio (.vsdx) output, standard library only (zipfile, xml.etree, zlib).

write(mxfile, path) turns the finished draw.io diagram - every page, layer
and cell that renderer.py and the feature plugins produced - into a Visio
drawing with the same layout. It reads the draw.io XML rather than the
topology, so a feature only ever draws once, and both files always match:

  draw.io                          Visio
  diagram (page)                   page, same name
  layer                            layer, same name, visibility and lock
  vertex: rectangle, rounded,      shape with the same geometry, fill,
    ellipse, text label            line, text, font size and rotation
  vertex with an image (icons)     picture (PNG; an SVG icon that only
                                   wraps a PNG is unwrapped; any other
                                   image becomes a plain box)
  edge                             a line through exactly the routed points
                                   (exit point, waypoints, entry point),
                                   dashed and arrowed as in draw.io
  label on an edge (interface      a shape at the same point along the
    labels, status dots)           line
  tooltip                          ScreenTip (the Comment cell)
  link to a page                   hyperlink to that page (a custom
                                   action's "open" page is used; the
                                   rest of the action, a highlight, has no
                                   Visio equivalent)
  other <object> attributes, tags  Shape Data

A .vsdx is an OPC package: a zip of XML parts tied together by
relationship files. This writes only the parts Visio needs: the document
(with one "No Style" style sheet that every shape uses), the pages list,
one part per page, images under visio/media/ (each distinct image once,
however many nodes show it), and the document
properties. Visio's y axis points up and its unit is the inch; draw.io's
y axis points down, and PX_PER_INCH draw.io units make an inch.

Not carried over: draw.io's line jumps at crossings, curved edges (drawn
as straight segments through the same points), and HTML formatting inside
a label (its text is kept, with line breaks).
"""
from __future__ import annotations

import base64
import html
import json
import math
import re
import struct
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
import zlib
from datetime import datetime, timezone
from xml.sax.saxutils import escape, quoteattr

PX_PER_INCH = 96.0
MARGIN_IN = 0.5

NS = "http://schemas.microsoft.com/office/visio/2012/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
VISIO_REL = "http://schemas.microsoft.com/visio/2010/relationships"
IMAGE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"

# draw.io's defaults for what our styles leave unset
_DEFAULT_FONT_PX = 12.0
_DEFAULT_ARC = 15.0          # percent of the shorter side, for rounded=1
# draw.io arrow -> Visio line end (BeginArrow/EndArrow index)
_ARROWS = {"none": 0, "": 0, "classic": 5, "classicThin": 5, "block": 4, "blockThin": 4,
           "open": 1, "openThin": 1, "oval": 10, "diamond": 22, "diamondThin": 22}
_BLANK = "​"


# ---------------------------------------------------------------------------
# Reading the draw.io side
# ---------------------------------------------------------------------------

def _style(s: str | None) -> dict:
    """'text;html=1;fillColor=#fff;' -> {'text': '', 'html': '1', 'fillColor': '#fff'}.
    A bare token (the first, usually) is a named style or shape."""
    out = {}
    for part in (s or "").split(";"):
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
        else:
            out[part] = ""
    return out


def _model(diagram: ET.Element) -> ET.Element:
    """A page's mxGraphModel, inflating a compressed page if need be."""
    model = diagram.find("mxGraphModel")
    if model is not None:
        return model
    data = (diagram.text or "").strip()
    raw = zlib.decompress(base64.b64decode(data), -15).decode("utf-8")
    return ET.fromstring(urllib.parse.unquote(raw))


class _Cell:
    def __init__(self, el: ET.Element):
        cell = el if el.tag == "mxCell" else el.find("mxCell")
        if cell is None:
            cell = ET.Element("mxCell")
        self.id = el.get("id")
        self.wrapped = el.tag != "mxCell"
        self.value = el.get("label") if self.wrapped else cell.get("value")
        self.attrs = {k: v for k, v in el.attrib.items()
                      if k not in ("id", "label", "placeholders")} if self.wrapped else {}
        self.style = _style(cell.get("style"))
        self.parent = cell.get("parent")
        self.vertex = cell.get("vertex") == "1"
        self.edge = cell.get("edge") == "1"
        self.source, self.target = cell.get("source"), cell.get("target")
        self.visible = cell.get("visible") != "0"
        geo = cell.find("mxGeometry")
        self.geo = geo
        g = geo.attrib if geo is not None else {}
        self.x, self.y = float(g.get("x", 0)), float(g.get("y", 0))
        self.w, self.h = float(g.get("width", 0)), float(g.get("height", 0))
        self.relative = g.get("relative") == "1"
        self.points, self.offset = [], (0.0, 0.0)
        self.source_point = self.target_point = None
        if geo is not None:
            arr = geo.find("Array[@as='points']")
            if arr is not None:
                self.points = [(float(p.get("x", 0)), float(p.get("y", 0)))
                               for p in arr.findall("mxPoint")]
            for p in geo.findall("mxPoint"):
                pt = (float(p.get("x", 0)), float(p.get("y", 0)))
                role = p.get("as")
                if role == "offset":
                    self.offset = pt
                elif role == "sourcePoint":
                    self.source_point = pt
                elif role == "targetPoint":
                    self.target_point = pt


def _text(value: str | None, is_html: bool) -> str:
    if not value:
        return ""
    text = value
    if is_html:
        # a block element starts and ends a line; runs of them make one break
        text = re.sub(r"(?i)</?(div|p|tr|li|h\d)\b[^>]*>", "\x00", text)
        text = re.sub("\x00+", "\x00", text).strip("\x00").replace("\x00", "\n")
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</t[dh]>", "  ", text)
        text = re.sub(r"<[^>]+>", "", text)
        text = html.unescape(text)
    text = text.replace(_BLANK, "").replace(" ", " ")
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
    return "\n".join(line.rstrip() for line in text.strip("\n").split("\n"))


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def _along(points, fraction):
    """The point at `fraction` (0..1) of a polyline's length."""
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


def _curve(points, steps=8):
    """draw.io's curved edge as a polyline: a quadratic curve through the
    midpoints of the waypoints, each waypoint being a control point."""
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


def _clip(rect, toward):
    """Where the line from rect's centre toward a point leaves the rect."""
    x, y, w, h = rect
    cx, cy = x + w / 2, y + h / 2
    dx, dy = toward[0] - cx, toward[1] - cy
    if (dx == 0 and dy == 0) or w == 0 or h == 0:
        return cx, cy
    t = min(abs((w / 2) / dx) if dx else math.inf, abs((h / 2) / dy) if dy else math.inf)
    t = min(t, 1.0)
    return cx + dx * t, cy + dy * t


def _text_size(text: str, font_px: float) -> tuple[float, float]:
    lines = text.split("\n") if text else [""]
    width = max(len(line) for line in lines) * font_px * 0.6 + 6
    return max(width, 8.0), len(lines) * font_px * 1.25 + 4


def _png_size(data: bytes) -> tuple[int, int] | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    return None


def _png_from_style(style: dict) -> bytes | None:
    """The PNG behind a draw.io image style: a PNG data URI, or an SVG data
    URI that only wraps one embedded PNG (as the icon library's do)."""
    image = style.get("image") or ""
    m = re.match(r"data:image/(png|svg\+xml),(.*)$", image, re.S)
    if not m:
        return None
    try:
        payload = base64.b64decode(m.group(2))
    except ValueError:
        return None
    if m.group(1) == "png":
        return payload if _png_size(payload) else None
    svg = payload.decode("utf-8", "replace")
    found = re.findall(r'<image\b[^>]*href="data:image/png;base64,([^"]+)"', svg)
    drawn = set(re.findall(r"<(\w+)", svg)) - {"svg", "image", "defs", "g", "title", "desc"}
    if len(found) == 1 and not drawn:
        png = base64.b64decode(found[0])
        return png if _png_size(png) else None
    return None


# ---------------------------------------------------------------------------
# Writing the Visio side
# ---------------------------------------------------------------------------

def _num(v: float) -> str:
    return f"{v:.6f}".rstrip("0").rstrip(".") if v != 0 else "0"


def _c(name, value, unit=None):
    v = _num(value) if isinstance(value, float) else str(value)
    return f'<Cell N="{name}" V={quoteattr(v)}' + (f' U="{unit}"' if unit else "") + "/>"


def _color(value: str | None, default=None):
    if not value or value in ("none", "default"):
        return default
    v = value.strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", v):
        return v.upper()
    if re.fullmatch(r"#[0-9a-fA-F]{3}", v):
        return "#" + "".join(ch * 2 for ch in v[1:]).upper()
    named = {"white": "#FFFFFF", "black": "#000000", "red": "#FF0000", "green": "#008000",
             "blue": "#0000FF", "gray": "#808080", "grey": "#808080"}
    return named.get(v.lower(), default)


def _rect_rows(w: float, h: float, r: float) -> list[str]:
    """Geometry rows for a w x h rectangle, its corners rounded with radius r
    (ArcTo rows: A is the arc's bow, the distance from the chord's middle to
    the arc's; positive bulges outward when going round counterclockwise)."""
    if r <= 0:
        pts = [("MoveTo", 0.0, 0.0), ("LineTo", w, 0.0), ("LineTo", w, h),
               ("LineTo", 0.0, h), ("LineTo", 0.0, 0.0)]
        return [f'<Row T="{t}" IX="{i + 1}">{_c("X", x)}{_c("Y", y)}</Row>'
                for i, (t, x, y) in enumerate(pts)]
    bow = r * (1 - 1 / math.sqrt(2))
    pts = [("MoveTo", r, 0.0, None), ("LineTo", w - r, 0.0, None), ("ArcTo", w, r, bow),
           ("LineTo", w, h - r, None), ("ArcTo", w - r, h, bow), ("LineTo", r, h, None),
           ("ArcTo", 0.0, h - r, bow), ("LineTo", 0.0, r, None), ("ArcTo", r, 0.0, bow)]
    return [f'<Row T="{t}" IX="{i + 1}">{_c("X", x)}{_c("Y", y)}'
            + (_c("A", a) if a is not None else "") + "</Row>"
            for i, (t, x, y, a) in enumerate(pts)]


def _row_name(name: str, used: set) -> str:
    base = re.sub(r"[^A-Za-z0-9_]", "_", name) or "Data"
    if base[0].isdigit():
        base = "_" + base
    out, n = base, 2
    while out in used:
        out, n = f"{base}_{n}", n + 1
    used.add(out)
    return out


class _PageWriter:
    def __init__(self, page_names: dict, media: dict):
        self.page_names = page_names    # draw.io diagram id -> page name
        self.media = media              # shared {png bytes: part name}
        self.rels: list[tuple[str, str]] = []   # (rId, target) for this page
        self.shapes: list[str] = []
        self.next_id = 1

    # -- coordinate transform -------------------------------------------
    def set_frame(self, min_x, min_y, max_x, max_y):
        self.min_x, self.min_y = min_x, min_y
        self.width = (max_x - min_x) / PX_PER_INCH + 2 * MARGIN_IN
        self.height = (max_y - min_y) / PX_PER_INCH + 2 * MARGIN_IN

    def X(self, x):
        return (x - self.min_x) / PX_PER_INCH + MARGIN_IN

    def Y(self, y):
        return self.height - ((y - self.min_y) / PX_PER_INCH + MARGIN_IN)

    # -- pieces shared by every shape -------------------------------------
    def _extras(self, cell: _Cell):
        """The ScreenTip cell, and the Shape Data and Hyperlink sections."""
        cells, sections = [], []
        tip = cell.attrs.get("tooltip")
        if tip:
            cells.append(_c("Comment", tip))
        data = {k: v for k, v in cell.attrs.items() if k not in ("tooltip", "link")}
        if data:
            used: set = set()
            sections.append('<Section N="Property">' + "".join(
                f'<Row N="{_row_name(k, used)}">{_c("Value", v, "STR")}{_c("Label", k)}'
                f'{_c("Type", 0)}</Row>' for k, v in data.items()) + "</Section>")
        link = self._hyperlink(cell.attrs.get("link"))
        if link:
            address, sub, desc = link
            sections.append('<Section N="Hyperlink"><Row N="Row_1">' + _c("Description", desc)
                            + _c("Address", address) + _c("SubAddress", sub)
                            + _c("NewWindow", 0) + _c("Default", 1) + "</Row></Section>")
        return cells, sections

    def _hyperlink(self, link: str | None):
        if not link:
            return None
        page = None
        if link.startswith("data:page/id,"):
            page = link[len("data:page/id,"):]
        elif link.startswith("data:action/json,"):
            try:
                actions = json.loads(link[len("data:action/json,"):]).get("actions", [])
            except ValueError:
                actions = []
            for a in actions:
                target = a.get("open") if isinstance(a, dict) else None
                if isinstance(target, str) and target.startswith("data:page/id,"):
                    page = target[len("data:page/id,"):]
                    break
        elif re.match(r"https?://", link):
            return link, "", link
        if page and page in self.page_names:
            name = self.page_names[page]
            return "", name, name
        return None

    def _text_cells(self, cell: _Cell, text: str, below: bool, w_in: float, h_in: float):
        """The text block's cells, and its Character and Paragraph sections."""
        st = cell.style
        out = []
        font_px = float(st.get("fontSize", _DEFAULT_FONT_PX))
        bits = int(st.get("fontStyle", "0") or 0) & 7
        color = _color(st.get("fontColor"), "#000000")
        align = {"left": 0, "center": 1, "right": 2}.get(st.get("align", "center"), 1)
        valign = {"top": 0, "middle": 1, "bottom": 2}.get(st.get("verticalAlign", "middle"), 1)
        if below:
            tw, th = max(w_in * 1.6, 1.0), (font_px * 1.4 + 4) / PX_PER_INCH
            out += [_c("TxtPinX", w_in / 2, "IN"), _c("TxtPinY", -th / 2, "IN"),
                    _c("TxtWidth", tw, "IN"), _c("TxtHeight", th, "IN"),
                    _c("TxtLocPinX", tw / 2, "IN"), _c("TxtLocPinY", th / 2, "IN")]
            valign = 0
        pad = 1.0 / 72
        out += [_c("VerticalAlign", valign), _c("LeftMargin", pad, "PT"),
                _c("RightMargin", pad, "PT"), _c("TopMargin", pad, "PT"),
                _c("BottomMargin", pad, "PT")]
        sections = ['<Section N="Character"><Row IX="0">' + _c("Color", color) + _c("Style", bits)
                    + _c("Size", font_px / PX_PER_INCH, "PT") + "</Row></Section>",
                    '<Section N="Paragraph"><Row IX="0">' + _c("HorzAlign", align)
                    + "</Row></Section>"]
        return out, sections

    def _add(self, kind, cells, sections, text="", foreign=""):
        """Append one shape. Visio's schema fixes the order of a shape's
        children - every Cell, then every Section, then Text, then
        ForeignData - and Visio refuses a file that breaks it."""
        sid = self.next_id
        self.next_id += 1
        self.shapes.append(
            f'<Shape ID="{sid}" Type="{kind}" LineStyle="0" FillStyle="0" TextStyle="0">'
            + "".join(cells) + "".join(sections)
            + (f"<Text>{escape(text)}</Text>" if text else "") + foreign + "</Shape>")

    # -- shapes -----------------------------------------------------------
    def box(self, cell: _Cell, rect, layer: str | None, text: str):
        st = cell.style
        x, y, w, h = rect
        w_in, h_in = w / PX_PER_INCH, h / PX_PER_INCH
        rotation = float(st.get("rotation", 0) or 0)
        is_text = "text" in st or st.get("shape") == "text"
        png = _png_from_style(st) if (st.get("shape") == "image" or "image" in st) else None

        if is_text:
            fill = _color(st.get("fillColor")) or _color(st.get("labelBackgroundColor"))
            line = _color(st.get("strokeColor"))
            if line is None and st.get("labelBackgroundColor") and st.get("labelBorderColor"):
                line = _color(st.get("labelBorderColor"), "#000000")
        elif png is not None:
            fill, line = None, None
        else:
            fill = _color(st.get("fillColor"), "#FFFFFF")
            line = _color(st.get("strokeColor"), "#000000") if st.get("strokeColor") != "none" else None

        out = []   # this shape's cells
        if png:
            # letterbox the picture inside the node's box, as draw.io does
            iw, ih = _png_size(png)
            scale = min(w / iw, h / ih) if iw and ih else 1.0
            pw, ph = iw * scale, ih * scale
            x, y = x + (w - pw) / 2, y + (h - ph) / 2
            w, h = pw, ph
            w_in, h_in = w / PX_PER_INCH, h / PX_PER_INCH
        out += [_c("PinX", self.X(x + w / 2), "IN"), _c("PinY", self.Y(y + h / 2), "IN"),
                _c("Width", w_in, "IN"), _c("Height", h_in, "IN"),
                _c("LocPinX", w_in / 2, "IN"), _c("LocPinY", h_in / 2, "IN")]
        if rotation:
            out.append(_c("Angle", -math.radians(rotation), "DEG"))
        if layer is not None:
            out.append(_c("LayerMember", layer))
        out += [_c("FillPattern", 1 if fill else 0)]
        if fill:
            out.append(_c("FillForegnd", fill))
        out += [_c("LinePattern", (2 if st.get("dashed") == "1" else 1) if line else 0)]
        if line:
            out += [_c("LineColor", line),
                    _c("LineWeight", float(st.get("strokeWidth", 1)) * 0.75 / 72, "PT")]
        radius = 0.0
        if st.get("rounded") == "1" and "ellipse" not in st:
            # draw.io: radius = arcSize% of the shorter side, at most half of it.
            # Drawn as arcs in the geometry rather than with the Rounding cell,
            # so viewers that ignore Rounding still show the rounded corners.
            arc = float(st.get("arcSize", _DEFAULT_ARC))
            radius = min(min(w_in, h_in) * arc / 100, min(w_in, h_in) / 2)
        if png:
            out += [_c("ImgOffsetX", 0.0, "IN"), _c("ImgOffsetY", 0.0, "IN"),
                    _c("ImgWidth", w_in, "IN"), _c("ImgHeight", h_in, "IN")]
        sections = []
        below = st.get("verticalLabelPosition") == "bottom"
        if text or below:
            text_cells, text_sections = self._text_cells(cell, text, below, w_in, h_in)
            out += text_cells
            sections += text_sections
        extra_cells, extra_sections = self._extras(cell)
        out += extra_cells
        sections += extra_sections
        if "ellipse" in st:
            rows = [f'<Row T="Ellipse" IX="1">{_c("X", w_in / 2)}{_c("Y", h_in / 2)}'
                    f'{_c("A", w_in)}{_c("B", h_in / 2)}{_c("C", w_in / 2)}{_c("D", h_in)}</Row>']
        else:
            rows = _rect_rows(w_in, h_in, radius)
        sections.append('<Section N="Geometry" IX="0">' + _c("NoFill", 0 if fill else 1)
                        + _c("NoLine", 0 if line else 1) + "".join(rows) + "</Section>")
        foreign = ""
        if png:
            # each distinct picture is stored once, however many nodes use it
            part = self.media.get(png)
            if part is None:
                part = self.media[png] = f"image{len(self.media) + 1}.png"
            target = f"../media/{part}"
            rid = next((r for r, t in self.rels if t == target), None)
            if rid is None:
                rid = f"rId{len(self.rels) + 1}"
                self.rels.append((rid, target))
            foreign = (f'<ForeignData ForeignType="Bitmap" CompressionType="PNG">'
                       f'<Rel r:id="{rid}"/></ForeignData>')
        self._add("Foreign" if png else "Shape", out, sections, text, foreign)

    def line(self, cell: _Cell, points, layer: str | None):
        st = cell.style
        if len(points) < 2:
            return
        xs, ys = [self.X(p[0]) for p in points], [self.Y(p[1]) for p in points]
        left, bottom = min(xs), min(ys)
        w_in, h_in = max(max(xs) - left, 0.01), max(max(ys) - bottom, 0.01)
        color = _color(st.get("strokeColor"), "#000000")
        dashed = st.get("dashed") == "1"
        out = [_c("PinX", left + w_in / 2, "IN"), _c("PinY", bottom + h_in / 2, "IN"),
                _c("Width", w_in, "IN"), _c("Height", h_in, "IN"),
                _c("LocPinX", w_in / 2, "IN"), _c("LocPinY", h_in / 2, "IN")]
        if layer is not None:
            out.append(_c("LayerMember", layer))
        out += [_c("FillPattern", 0), _c("LinePattern", 2 if dashed else 1),
                _c("LineColor", color),
                _c("LineWeight", float(st.get("strokeWidth", 1)) * 0.75 / 72, "PT"),
                _c("BeginArrow", _ARROWS.get(st.get("startArrow", "none"), 5)),
                _c("EndArrow", _ARROWS.get(st.get("endArrow", "classic"), 5)),
                _c("BeginArrowSize", 1), _c("EndArrowSize", 1)]
        extra_cells, sections = self._extras(cell)
        out += extra_cells
        sections.append('<Section N="Geometry" IX="0">' + _c("NoFill", 1) + _c("NoLine", 0) + "".join(
            f'<Row T="{"MoveTo" if i == 0 else "LineTo"}" IX="{i + 1}">'
            f'{_c("X", px - left)}{_c("Y", py - bottom)}</Row>'
            for i, (px, py) in enumerate(zip(xs, ys))) + "</Section>")
        self._add("Shape", out, sections)

    def xml(self) -> str:
        return (f'<?xml version="1.0" encoding="utf-8" standalone="yes"?>'
                f'<PageContents xmlns="{NS}" xmlns:r="{R_NS}" xml:space="preserve">'
                f'<Shapes>{"".join(self.shapes)}</Shapes></PageContents>')

    def rels_xml(self) -> str | None:
        if not self.rels:
            return None
        rows = "".join(f'<Relationship Id="{rid}" Type="{IMAGE_REL}" Target="{t}"/>'
                       for rid, t in self.rels)
        return f'<?xml version="1.0" encoding="utf-8" standalone="yes"?><Relationships xmlns="{PKG_REL_NS}">{rows}</Relationships>'


def _convert_page(model: ET.Element, writer: _PageWriter) -> tuple[list[dict], dict]:
    """Fill `writer` with one draw.io page's cells. Returns the page's
    layers [{name, visible, locked}] for its PageSheet."""
    root = model.find("root")
    cells = [_Cell(el) for el in root] if root is not None else []
    by_id = {c.id: c for c in cells}
    top = next((c.id for c in cells if c.parent is None), None)
    layers = [c for c in cells if c.parent == top and c.id != top]
    layer_index = {c.id: i for i, c in enumerate(layers)}

    def layer_of(cell):
        seen = set()
        while cell is not None and cell.parent != top and cell.id not in seen:
            seen.add(cell.id)
            cell = by_id.get(cell.parent)
        return str(layer_index[cell.id]) if cell is not None and cell.id in layer_index else None

    rects: dict[str, tuple] = {}

    def rect(cid):
        if cid in rects:
            return rects[cid]
        c = by_id.get(cid)
        if c is None or not c.vertex:
            return None
        x, y = c.x, c.y
        parent = by_id.get(c.parent)
        if parent is not None and parent.vertex and not c.relative:
            pr = rect(parent.id)
            if pr:
                x, y = x + pr[0], y + pr[1]
        rects[cid] = (x, y, c.w, c.h)
        return rects[cid]

    def route(c: _Cell):
        src, dst = rect(c.source), rect(c.target)
        pts = list(c.points)
        if src is None and c.source_point is None or dst is None and c.target_point is None:
            return []

        def end(r, st_x, st_y, dx, dy, toward, fixed):
            if r is None:
                return fixed
            fx, fy = c.style.get(st_x), c.style.get(st_y)
            if fx is not None and fy is not None:
                return (r[0] + float(fx) * r[2] + float(c.style.get(dx, 0) or 0),
                        r[1] + float(fy) * r[3] + float(c.style.get(dy, 0) or 0))
            return _clip(r, toward)

        dst_center = (dst[0] + dst[2] / 2, dst[1] + dst[3] / 2) if dst else c.target_point
        src_center = (src[0] + src[2] / 2, src[1] + src[3] / 2) if src else c.source_point
        start = end(src, "exitX", "exitY", "exitDx", "exitDy",
                    pts[0] if pts else dst_center, c.source_point)
        finish = end(dst, "entryX", "entryY", "entryDx", "entryDy",
                     pts[-1] if pts else src_center, c.target_point)
        line = [start] + pts + [finish]
        return _curve(line) if c.style.get("curved") == "1" else line

    routes = {c.id: route(c) for c in cells if c.edge}

    # where every drawn cell goes, in draw.io units (also gives the page bounds)
    placed = []   # (kind, cell, geometry, layer, text)
    for c in cells:
        if c.id in layer_index or c.id == top:
            continue
        layer = layer_of(c)
        if c.edge:
            if routes.get(c.id):
                placed.append(("line", c, routes[c.id], layer, ""))
            continue
        if not c.vertex:
            continue
        text = _text(c.value, c.style.get("html") == "1")
        parent = by_id.get(c.parent)
        if parent is not None and parent.edge and c.relative:
            pts = routes.get(parent.id) or []
            if not pts:
                continue
            px, py = _along(pts, (c.x + 1) / 2)
            if c.w and c.h:
                r = (px + c.offset[0], py + c.offset[1], c.w, c.h)
            else:
                w, h = _text_size(text, float(c.style.get("fontSize", _DEFAULT_FONT_PX)))
                r = (px + c.offset[0] - w / 2, py + c.offset[1] - h / 2, w, h)
        else:
            r = rect(c.id)
            if r is None:
                continue
            if not r[2] or not r[3]:
                w, h = _text_size(text, float(c.style.get("fontSize", _DEFAULT_FONT_PX)))
                r = (r[0], r[1], r[2] or w, r[3] or h)
        placed.append(("box", c, r, layer, text))

    xs, ys = [], []
    for kind, c, geom, *_ in placed:
        if kind == "line":
            xs += [p[0] for p in geom]
            ys += [p[1] for p in geom]
        else:
            x, y, w, h = geom
            extra = 30 if c.style.get("verticalLabelPosition") == "bottom" else 0
            xs += [x, x + w]
            ys += [y, y + h + extra]
    writer.set_frame(min(xs, default=0), min(ys, default=0), max(xs, default=0), max(ys, default=0))
    for kind, c, geom, layer, text in placed:
        if kind == "line":
            writer.line(c, geom, layer)
        else:
            writer.box(c, geom, layer, text)

    return [{"name": (_text(c.value, True) or ("Background" if i == 0 else f"Layer {i}")),
             "visible": c.visible,
             "locked": _style_has(c, "locked")} for i, c in enumerate(layers)]


def _style_has(cell: _Cell, key: str) -> bool:
    return cell.style.get(key) == "1"


def _page_sheet(writer: _PageWriter, layers: list[dict]) -> str:
    out = ["<PageSheet LineStyle=\"0\" FillStyle=\"0\" TextStyle=\"0\">",
           _c("PageWidth", writer.width, "IN"), _c("PageHeight", writer.height, "IN"),
           _c("PageScale", 1.0, "IN"), _c("DrawingScale", 1.0, "IN"),
           _c("DrawingSizeType", 3), _c("DrawingScaleType", 0), _c("InhibitSnap", 0),
           _c("PageLockReplace", 0), _c("PageLockDuplicate", 0), _c("UIVisibility", 0),
           _c("ShdwType", 0), _c("ShdwObliqueAngle", 0), _c("ShdwScaleFactor", 1),
           _c("PrintPageOrientation", 2 if writer.width > writer.height else 1)]
    if layers:
        out.append('<Section N="Layer">')
        for i, lay in enumerate(layers):
            out.append(f'<Row IX="{i}">' + _c("Name", lay["name"]) + _c("NameUniv", lay["name"])
                       + _c("Color", 255) + _c("Status", 0) + _c("Visible", int(lay["visible"]))
                       + _c("Print", int(lay["visible"])) + _c("Active", 0)
                       + _c("Lock", int(lay["locked"])) + _c("Snap", 1) + _c("Glue", 1)
                       + _c("ColorTrans", 0.0) + "</Row>")
        out.append("</Section>")
    out.append("</PageSheet>")
    return "".join(out)


_STYLE_SHEET = (
    '<StyleSheets><StyleSheet ID="0" NameU="No Style" Name="No Style">'
    + "".join(_c(n, v, u) for n, v, u in [
        ("EnableLineProps", 1, None), ("EnableFillProps", 1, None), ("EnableTextProps", 1, None),
        ("HideForApply", 0, None), ("LineWeight", 0.75 / 72, "PT"), ("LineColor", "#000000", None),
        ("LinePattern", 1, None), ("Rounding", 0.0, "IN"), ("EndArrowSize", 1, None),
        ("BeginArrow", 0, None), ("EndArrow", 0, None), ("LineCap", 0, None),
        ("BeginArrowSize", 1, None), ("LineColorTrans", 0.0, None), ("CompoundType", 0, None),
        ("FillForegnd", "#FFFFFF", None), ("FillBkgnd", "#000000", None), ("FillPattern", 1, None),
        ("ShdwForegnd", "#000000", None), ("ShdwPattern", 0, None), ("FillForegndTrans", 0.0, None),
        ("FillBkgndTrans", 0.0, None), ("ShdwForegndTrans", 0.0, None), ("ShapeShdwType", 0, None),
        ("ShapeShdwOffsetX", 0.0, "IN"), ("ShapeShdwOffsetY", 0.0, "IN"),
        ("ShapeShdwObliqueAngle", 0.0, None), ("ShapeShdwScaleFactor", 1, None),
        ("ShapeShdwBlur", 0.0, None), ("ShapeShdwShow", 0, None), ("LeftMargin", 0.0, "PT"),
        ("RightMargin", 0.0, "PT"), ("TopMargin", 0.0, "PT"), ("BottomMargin", 0.0, "PT"),
        ("VerticalAlign", 1, None), ("TextBkgnd", 0, None), ("DefaultTabStop", 0.5, "IN"),
        ("TextDirection", 0, None), ("TextBkgndTrans", 0.0, None), ("LockTextEdit", 0, None),
        ("NoObjHandles", 0, None), ("NonPrinting", 0, None), ("NoAlignBox", 0, None),
        ("HideText", 0, None), ("ObjType", 0, None)])
    + '<Section N="Character"><Row IX="0">' + _c("Font", "Calibri") + _c("Color", "#000000")
    + _c("Style", 0) + _c("Size", 12 / 96, "PT") + "</Row></Section>"
    + '<Section N="Paragraph"><Row IX="0">' + _c("HorzAlign", 1) + "</Row></Section>"
    + "</StyleSheet></StyleSheets>")


def write(mxfile: ET.Element, path) -> dict:
    """Write the draw.io document `mxfile` to `path` as .vsdx. Returns
    {"pages": n, "shapes": n, "images": n}."""
    diagrams = mxfile.findall("diagram")
    page_names, used = {}, set()
    for i, d in enumerate(diagrams):
        name = d.get("name") or f"Page-{i + 1}"
        base, n = name, 2
        while name in used:
            name, n = f"{base} ({n})", n + 1
        used.add(name)
        page_names[d.get("id") or f"__page{i}"] = name

    media: dict = {}
    pages = []   # (name, writer, layers)
    for i, d in enumerate(diagrams):
        writer = _PageWriter(page_names, media)
        layers = _convert_page(_model(d), writer)
        pages.append((page_names[d.get("id") or f"__page{i}"], writer, layers))

    if not pages:   # Visio needs at least one page
        writer = _PageWriter(page_names, media)
        writer.set_frame(0, 0, 0, 0)
        pages.append(("Page-1", writer, []))
    first_w, first_h = pages[0][1].width, pages[0][1].height
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    head = '<?xml version="1.0" encoding="utf-8" standalone="yes"?>'
    parts = {
        "[Content_Types].xml": head
        + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="png" ContentType="image/png"/>'
        '<Override PartName="/visio/document.xml" ContentType="application/vnd.ms-visio.drawing.main+xml"/>'
        '<Override PartName="/visio/pages/pages.xml" ContentType="application/vnd.ms-visio.pages+xml"/>'
        '<Override PartName="/visio/windows.xml" ContentType="application/vnd.ms-visio.windows+xml"/>'
        + "".join(f'<Override PartName="/visio/pages/page{i + 1}.xml" '
                  'ContentType="application/vnd.ms-visio.page+xml"/>' for i in range(len(pages)))
        + '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        "</Types>",
        "_rels/.rels": head + f'<Relationships xmlns="{PKG_REL_NS}">'
        f'<Relationship Id="rId1" Type="{VISIO_REL}/document" Target="visio/document.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
        "</Relationships>",
        "docProps/core.xml": head
        + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        "<dc:title>Network topology</dc:title><dc:creator>autotop-awx</dc:creator>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>'
        "</cp:coreProperties>",
        "docProps/app.xml": head
        + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
        "<Application>autotop-awx</Application></Properties>",
        "visio/document.xml": head
        + f'<VisioDocument xmlns="{NS}" xmlns:r="{R_NS}" xml:space="preserve">'
        '<DocumentSettings TopPage="0" DefaultTextStyle="0" DefaultLineStyle="0" '
        'DefaultFillStyle="0" DefaultGuideStyle="0"><GlueSettings>9</GlueSettings>'
        "<SnapSettings>65847</SnapSettings><SnapExtensions>34</SnapExtensions><SnapAngles/>"
        "<DynamicGridEnabled>0</DynamicGridEnabled><ProtectStyles>0</ProtectStyles>"
        "<ProtectShapes>0</ProtectShapes><ProtectMasters>0</ProtectMasters>"
        "<ProtectBkgnds>0</ProtectBkgnds></DocumentSettings>"
        '<Colors><ColorEntry IX="0" RGB="#000000"/><ColorEntry IX="1" RGB="#FFFFFF"/></Colors>'
        '<FaceNames><FaceName NameU="Calibri" UnicodeRanges="-469750017 -1040178053 9 0" '
        'CharSets="536871423 0" Panose="2 15 5 2 2 2 4 3 2 4" Flags="325"/></FaceNames>'
        + _STYLE_SHEET + "</VisioDocument>",
        "visio/_rels/document.xml.rels": head + f'<Relationships xmlns="{PKG_REL_NS}">'
        f'<Relationship Id="rId1" Type="{VISIO_REL}/pages" Target="pages/pages.xml"/>'
        f'<Relationship Id="rId2" Type="{VISIO_REL}/windows" Target="windows.xml"/>'
        "</Relationships>",
        # the window Visio opens with: the first page, whole page in view
        "visio/windows.xml": head
        + f'<Windows ClientWidth="1280" ClientHeight="800" xmlns="{NS}" xmlns:r="{R_NS}" '
        'xml:space="preserve"><Window ID="0" WindowType="Drawing" WindowState="1073741824" '
        'WindowLeft="0" WindowTop="0" WindowWidth="1280" WindowHeight="800" ContainerType="Page" '
        f'Page="0" ViewScale="-1" ViewCenterX={quoteattr(_num(first_w / 2))} '
        f'ViewCenterY={quoteattr(_num(first_h / 2))}>'
        "<ShowRulers>1</ShowRulers><ShowGrid>0</ShowGrid><ShowPageBreaks>0</ShowPageBreaks>"
        "<ShowGuides>1</ShowGuides><ShowConnectionPoints>0</ShowConnectionPoints>"
        "<GlueSettings>9</GlueSettings><SnapSettings>65847</SnapSettings>"
        "<SnapExtensions>34</SnapExtensions><SnapAngles/><DynamicGridEnabled>0</DynamicGridEnabled>"
        "<TabSplitterPos>0.5</TabSplitterPos></Window></Windows>",
        "visio/pages/pages.xml": head
        + f'<Pages xmlns="{NS}" xmlns:r="{R_NS}" xml:space="preserve">'
        + "".join(f'<Page ID="{i}" NameU={quoteattr(name)} Name={quoteattr(name)} ViewScale="-1">'
                  + _page_sheet(w, layers) + f'<Rel r:id="rId{i + 1}"/></Page>'
                  for i, (name, w, layers) in enumerate(pages))
        + "</Pages>",
        "visio/pages/_rels/pages.xml.rels": head + f'<Relationships xmlns="{PKG_REL_NS}">'
        + "".join(f'<Relationship Id="rId{i + 1}" Type="{VISIO_REL}/page" Target="page{i + 1}.xml"/>'
                  for i in range(len(pages)))
        + "</Relationships>",
    }
    shapes = 0
    for i, (name, w, layers) in enumerate(pages):
        parts[f"visio/pages/page{i + 1}.xml"] = w.xml()
        shapes += len(w.shapes)
        rels = w.rels_xml()
        if rels:
            parts[f"visio/pages/_rels/page{i + 1}.xml.rels"] = rels

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)
        for png, name in media.items():
            z.writestr(f"visio/media/{name}", png)
    return {"pages": len(pages), "shapes": shapes, "images": len(media)}
