"""
Microsoft Visio (.vsdx) writer, standard library only (zipfile, xml.etree).

write(diagram, path) writes a diagram.Diagram - the format-independent
drawing that renderer.py and the feature plugins build - as a Visio file.
It doesn't read or need the draw.io output: drawio.py is a separate writer
of the same Diagram.

  Diagram                          Visio
  Page                             page, same name, sized to its drawing
  Layer                            layer, same name, visibility and lock;
                                   base-layer items are on "Background"
  Shape: box, ellipse, text        shape with the same geometry, fill,
                                   outline, rounded corners, text, font
                                   size and rotation
  Shape with an Icon               picture (PNG, or the PNG an SVG icon
                                   wraps), letterboxed, name below; each
                                   distinct picture is stored once
  Line                             a line through exactly the routed points
                                   (Page.route), dashed and arrowed;
                                   curved lines as smooth polylines
  LineLabel                        a shape at its point along the line
  tooltip                          ScreenTip (the Comment cell)
  link to a page / web address     hyperlink (highlighting has no Visio
                                   equivalent: the page just opens)
  data, tags                       Shape Data

A .vsdx is an OPC package: a zip of XML parts tied together by
relationship files. This writes the document (with one "No Style" style
sheet every shape uses), the window it opens in, the pages list, one part
per page, images under visio/media/, and the document properties.
Visio checks every part against its schema and refuses a file that breaks
it ("some parts are missing or invalid"); in particular each shape's
children must come in order - Cells, Sections, Text, ForeignData.

Visio's y axis points up and its unit is the inch; the Diagram's y axis
points down, and PX_PER_INCH pixels make an inch. Each page records where
its top-left corner sits in diagram pixels (User.OriginX / User.OriginY in
its page sheet), so positions.py can read shape positions back. Lines are drawn as shapes
through their points, not as connectors glued to the shapes, so Visio
never reroutes them; line hops at crossings aren't drawn.
"""
from __future__ import annotations

import math
import re
import zipfile
from datetime import datetime, timezone
from xml.sax.saxutils import escape, quoteattr

import diagram as dg

PX_PER_INCH = 96.0
MARGIN_IN = 0.5

NS = "http://schemas.microsoft.com/office/visio/2012/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
VISIO_REL = "http://schemas.microsoft.com/visio/2010/relationships"
IMAGE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"

# arrow name -> Visio line end (BeginArrow/EndArrow index)
ARROWS = {"none": 0, "classic": 5, "block": 4, "open": 1, "oval": 10, "diamond": 22}


def _num(v: float) -> str:
    return f"{v:.6f}".rstrip("0").rstrip(".") if v != 0 else "0"


def _c(name, value, unit=None):
    v = _num(value) if isinstance(value, float) else str(value)
    return f'<Cell N="{name}" V={quoteattr(v)}' + (f' U="{unit}"' if unit else "") + "/>"


def _color(value: str | None, default=None):
    if not value or value == "none":
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
    def __init__(self, page_names: dict, media: dict, layer_index: dict):
        self.page_names = page_names    # Page.id -> Visio page name
        self.media = media              # shared {png bytes: part name}
        self.layer_index = layer_index  # Layer.id -> row in this page's Layer section
        self.rels: list = []            # (rId, target) for this page
        self.shapes: list = []
        self.next_id = 1

    # -- coordinate transform -------------------------------------------
    def set_frame(self, min_x, min_y, max_x, max_y):
        self.min_x, self.min_y = min_x, min_y
        self.width = (max_x - min_x) / PX_PER_INCH + 2 * MARGIN_IN
        self.height = (max_y - min_y) / PX_PER_INCH + 2 * MARGIN_IN

    @property
    def origin_x(self):
        return float(self.min_x - MARGIN_IN * PX_PER_INCH)

    @property
    def origin_y(self):
        return float(self.min_y - MARGIN_IN * PX_PER_INCH)

    def X(self, x):
        return (x - self.min_x) / PX_PER_INCH + MARGIN_IN

    def Y(self, y):
        return self.height - ((y - self.min_y) / PX_PER_INCH + MARGIN_IN)

    # -- pieces shared by every shape -------------------------------------
    def _layer_cells(self, item):
        return [_c("LayerMember", self.layer_index[item.layer])] if item.layer else []

    def _extras(self, item):
        """The ScreenTip cell, and the Shape Data and Hyperlink sections."""
        cells, sections = [], []
        if item.tooltip:
            cells.append(_c("Comment", item.tooltip))
        data = dict(item.data)
        if item.tags:
            data["tags"] = " ".join(item.tags)
        if data:
            used: set = set()
            sections.append('<Section N="Property">' + "".join(
                f'<Row N="{_row_name(k, used)}">{_c("Value", str(v), "STR")}{_c("Label", k)}'
                f'{_c("Type", 0)}</Row>' for k, v in data.items()) + "</Section>")
        link = dg.parse_link(item.link)
        target = None
        if link and link[0] == "page" and link[1] in self.page_names:
            name = self.page_names[link[1]]
            target = ("", name, name)
        elif link and link[0] == "url":
            target = (link[1], "", link[1])
        if target:
            address, sub, desc = target
            sections.append('<Section N="Hyperlink"><Row N="Row_1">' + _c("Description", desc)
                            + _c("Address", address) + _c("SubAddress", sub)
                            + _c("NewWindow", 0) + _c("Default", 1) + "</Row></Section>")
        return cells, sections

    def _text_parts(self, st: dict, below: bool, w_in: float):
        """The text block's cells, and its Character and Paragraph sections."""
        font_px = float(st.get("font_size", dg.DEFAULT_FONT_SIZE))
        cells = []
        valign = {"top": 0, "middle": 1, "bottom": 2}.get(st.get("valign", "middle"), 1)
        if below:
            tw, th = max(w_in * 1.6, 1.0), (font_px * 1.4 + 4) / PX_PER_INCH
            cells += [_c("TxtPinX", w_in / 2, "IN"), _c("TxtPinY", -th / 2, "IN"),
                      _c("TxtWidth", tw, "IN"), _c("TxtHeight", th, "IN"),
                      _c("TxtLocPinX", tw / 2, "IN"), _c("TxtLocPinY", th / 2, "IN")]
            valign = 0
        pad = max(float(st.get("spacing", 1)), 0) / PX_PER_INCH
        cells += [_c("VerticalAlign", valign), _c("LeftMargin", pad, "PT"),
                  _c("RightMargin", pad, "PT"), _c("TopMargin", pad, "PT"),
                  _c("BottomMargin", pad, "PT")]
        align = {"left": 0, "center": 1, "right": 2}.get(st.get("align", "center"), 1)
        sections = ['<Section N="Character"><Row IX="0">'
                    + _c("Color", _color(st.get("font_color"), "#000000"))
                    + _c("Style", 1 if st.get("bold") else 0)
                    + _c("Size", font_px / PX_PER_INCH, "PT") + "</Row></Section>",
                    '<Section N="Paragraph"><Row IX="0">' + _c("HorzAlign", align)
                    + "</Row></Section>"]
        return cells, sections

    def _add(self, kind, cells, sections, text="", foreign=""):
        """Append one shape. Visio's schema fixes the order of a shape's
        children - every Cell, then every Section, then Text, then
        ForeignData - and Visio refuses a file that breaks it."""
        sid = self.next_id
        self.next_id += 1
        self.shapes.append(
            f'<Shape ID="{sid}" Type="{kind}" LineStyle="0" FillStyle="0" TextStyle="0">'
            + "".join(cells) + "".join(sections)
            + (f"<Text>{escape(_clean(text))}</Text>" if text else "") + foreign + "</Shape>")

    # -- shapes -----------------------------------------------------------
    def box(self, item, rect):
        """A Shape, or a LineLabel at its placed rect."""
        st = item.style
        x, y, w, h = rect
        kind = st.get("kind", "box")
        icon = getattr(item, "icon", None)
        png = icon.png() if icon else None
        if kind == "text":
            fill = _color(st.get("fill")) or _color(st.get("label_bg"))
            line = _color(st.get("stroke")) or _color(st.get("label_border"))
        elif png is not None:
            fill = line = None
        else:
            fill = _color(st.get("fill"), "#FFFFFF")
            line = None if st.get("stroke") == "none" else _color(st.get("stroke"), "#000000")

        if png is not None:
            # letterbox the picture inside the node's box
            iw, ih = dg.png_size(png)
            scale = min(w / iw, h / ih) if iw and ih else 1.0
            pw, ph = iw * scale, ih * scale
            x, y, w, h = x + (w - pw) / 2, y + (h - ph) / 2, pw, ph
        w_in, h_in = w / PX_PER_INCH, h / PX_PER_INCH
        cells = [_c("PinX", self.X(x + w / 2), "IN"), _c("PinY", self.Y(y + h / 2), "IN"),
                 _c("Width", w_in, "IN"), _c("Height", h_in, "IN"),
                 _c("LocPinX", w_in / 2, "IN"), _c("LocPinY", h_in / 2, "IN")]
        if st.get("rotation"):
            cells.append(_c("Angle", -math.radians(float(st["rotation"])), "DEG"))
        cells += self._layer_cells(item)
        cells.append(_c("FillPattern", 1 if fill else 0))
        if fill:
            cells.append(_c("FillForegnd", fill))
        cells.append(_c("LinePattern", (2 if st.get("dashed") else 1) if line else 0))
        if line:
            cells += [_c("LineColor", line),
                      _c("LineWeight", float(st.get("stroke_width", 1)) * 0.75 / 72, "PT")]
        if png is not None:
            cells += [_c("ImgOffsetX", 0.0, "IN"), _c("ImgOffsetY", 0.0, "IN"),
                      _c("ImgWidth", w_in, "IN"), _c("ImgHeight", h_in, "IN")]
        sections = []
        below = bool(icon and icon.label_below and png is not None)
        if item.text or below:
            text_cells, text_sections = self._text_parts(st, below, w_in)
            cells += text_cells
            sections += text_sections
        extra_cells, extra_sections = self._extras(item)
        cells += extra_cells
        sections += extra_sections
        if kind == "ellipse":
            rows = [f'<Row T="Ellipse" IX="1">{_c("X", w_in / 2)}{_c("Y", h_in / 2)}'
                    f'{_c("A", w_in)}{_c("B", h_in / 2)}{_c("C", w_in / 2)}{_c("D", h_in)}</Row>']
        else:
            radius = 0.0
            if st.get("rounded"):
                # radius = arc% of the shorter side, at most half of it. Drawn
                # as arcs rather than with the Rounding cell, so viewers that
                # ignore Rounding still show the rounded corners.
                arc = float(st.get("arc", dg.DEFAULT_ARC))
                radius = min(min(w_in, h_in) * arc / 100, min(w_in, h_in) / 2)
            rows = _rect_rows(w_in, h_in, radius)
        sections.append('<Section N="Geometry" IX="0">' + _c("NoFill", 0 if fill else 1)
                        + _c("NoLine", 0 if line else 1) + "".join(rows) + "</Section>")
        foreign = ""
        if png is not None:
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
        self._add("Foreign" if png is not None else "Shape", cells, sections, item.text, foreign)

    def line(self, item, points):
        st = item.style
        if len(points) < 2:
            return
        xs, ys = [self.X(p[0]) for p in points], [self.Y(p[1]) for p in points]
        left, bottom = min(xs), min(ys)
        w_in, h_in = max(max(xs) - left, 0.01), max(max(ys) - bottom, 0.01)
        cells = [_c("PinX", left + w_in / 2, "IN"), _c("PinY", bottom + h_in / 2, "IN"),
                 _c("Width", w_in, "IN"), _c("Height", h_in, "IN"),
                 _c("LocPinX", w_in / 2, "IN"), _c("LocPinY", h_in / 2, "IN")]
        cells += self._layer_cells(item)
        cells += [_c("FillPattern", 0), _c("LinePattern", 2 if st.get("dashed") else 1),
                  _c("LineColor", _color(st.get("stroke"), "#000000")),
                  _c("LineWeight", float(st.get("stroke_width", 1)) * 0.75 / 72, "PT"),
                  _c("BeginArrow", ARROWS.get(st.get("start_arrow", "none"), 0)),
                  _c("EndArrow", ARROWS.get(st.get("end_arrow", "classic"), 5)),
                  _c("BeginArrowSize", 1), _c("EndArrowSize", 1)]
        extra_cells, sections = self._extras(item)
        cells += extra_cells
        sections.append('<Section N="Geometry" IX="0">' + _c("NoFill", 1) + _c("NoLine", 0) + "".join(
            f'<Row T="{"MoveTo" if i == 0 else "LineTo"}" IX="{i + 1}">'
            f'{_c("X", px - left)}{_c("Y", py - bottom)}</Row>'
            for i, (px, py) in enumerate(zip(xs, ys))) + "</Section>")
        self._add("Shape", cells, sections, item.text)

    def xml(self) -> str:
        return (f'<?xml version="1.0" encoding="utf-8" standalone="yes"?>'
                f'<PageContents xmlns="{NS}" xmlns:r="{R_NS}" xml:space="preserve">'
                f'<Shapes>{"".join(self.shapes)}</Shapes></PageContents>')

    def rels_xml(self) -> str | None:
        if not self.rels:
            return None
        rows = "".join(f'<Relationship Id="{rid}" Type="{IMAGE_REL}" Target="{t}"/>'
                       for rid, t in self.rels)
        return ('<?xml version="1.0" encoding="utf-8" standalone="yes"?>'
                f'<Relationships xmlns="{PKG_REL_NS}">{rows}</Relationships>')


def _clean(text: str) -> str:
    """Text without the control characters XML can't hold."""
    return "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)


def _label_rect(page, label):
    """Where a LineLabel goes: a sized label's top-left is its point, an
    unsized one is centred on it, sized to its text."""
    point = page.label_point(label)
    if point is None:
        return None
    if label.w and label.h:
        return point[0], point[1], label.w, label.h
    w, h = dg.text_size(label.text, float(label.style.get("font_size", dg.DEFAULT_FONT_SIZE)))
    return point[0] - w / 2, point[1] - h / 2, w, h


def _convert_page(page, writer: _PageWriter):
    """Fill `writer` with one page's items, in drawing order: the base
    layer first, then each layer in turn (Visio stacks by shape order)."""
    order = {None: -1, **{lay.id: i for i, lay in enumerate(page.layers)}}
    placed = []   # (kind, item, geometry)
    for item in sorted(page.items, key=lambda i: order.get(_layer_of(page, i), -1)):
        if isinstance(item, dg.Line):
            pts = page.route(item)
            if pts:
                placed.append(("line", item, pts))
        elif isinstance(item, dg.LineLabel):
            rect = _label_rect(page, item)
            if rect:
                placed.append(("box", item, rect))
        elif isinstance(item, dg.Shape):
            x, y, w, h = item.rect
            if not w or not h:
                tw, th = dg.text_size(item.text, float(item.style.get("font_size",
                                                                        dg.DEFAULT_FONT_SIZE)))
                w, h = w or tw, h or th
            placed.append(("box", item, (x, y, w, h)))

    xs, ys = [], []
    for kind, item, geom in placed:
        if kind == "line":
            xs += [p[0] for p in geom]
            ys += [p[1] for p in geom]
        else:
            x, y, w, h = geom
            icon = getattr(item, "icon", None)
            extra = 30 if icon is not None and icon.label_below else 0
            xs += [x, x + w]
            ys += [y, y + h + extra]
    writer.set_frame(min(xs, default=0), min(ys, default=0), max(xs, default=0), max(ys, default=0))
    for kind, item, geom in placed:
        if kind == "line":
            writer.line(item, geom)
        else:
            writer.box(item, geom)


def _layer_of(page, item):
    """A LineLabel is on its line's layer unless it names its own."""
    if isinstance(item, dg.LineLabel) and item.layer is None:
        line = page.get(item.line)
        return line.layer if line is not None else None
    return item.layer


def _page_sheet(writer: _PageWriter, layers: list[dict]) -> str:
    out = ["<PageSheet LineStyle=\"0\" FillStyle=\"0\" TextStyle=\"0\">",
           _c("PageWidth", writer.width, "IN"), _c("PageHeight", writer.height, "IN"),
           _c("PageScale", 1.0, "IN"), _c("DrawingScale", 1.0, "IN"),
           _c("DrawingSizeType", 3), _c("DrawingScaleType", 0), _c("InhibitSnap", 0),
           _c("PageLockReplace", 0), _c("PageLockDuplicate", 0), _c("UIVisibility", 0),
           _c("ShdwType", 0), _c("ShdwObliqueAngle", 0), _c("ShdwScaleFactor", 1),
           _c("PrintPageOrientation", 2 if writer.width > writer.height else 1)]
    # Where the page's top-left corner sits in diagram pixels, so a reader
    # (positions.py) can turn shape positions back into diagram coordinates
    out.append('<Section N="User">' + "".join(
        f'<Row N="{n}">' + _c("Value", v) + _c("Prompt", "") + "</Row>"
        for n, v in (("OriginX", writer.origin_x), ("OriginY", writer.origin_y))) + "</Section>")
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


def write(diagram: dg.Diagram, path) -> dict:
    """Write `diagram` to `path` (a file name or a binary file object) as
    .vsdx. Returns {"pages": n, "shapes": n, "images": n}."""
    page_names, used = {}, set()
    for i, page in enumerate(diagram.pages):
        name = page.name or f"Page-{i + 1}"
        base, n = name, 2
        while name in used:
            name, n = f"{base} ({n})", n + 1
        used.add(name)
        page_names[page.id] = name

    media: dict = {}
    pages = []   # (name, writer, layers)
    for page in diagram.pages:
        layers = [{"name": "Background", "visible": True, "locked": False}] + [
            {"name": lay.name, "visible": lay.visible, "locked": lay.locked}
            for lay in page.layers]
        writer = _PageWriter(page_names, media,
                             {lay.id: str(i + 1) for i, lay in enumerate(page.layers)})
        _convert_page(page, writer)
        pages.append((page_names[page.id], writer, layers))

    if not pages:   # Visio needs at least one page
        writer = _PageWriter(page_names, media, {})
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
