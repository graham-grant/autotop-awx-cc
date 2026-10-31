"""
Node positions: the position template, a Visio file (template.vsdx at the
project root by default).

The template holds one shape per node, at the node's position and size and
showing its icon, and nothing else: no links or labels, so nodes can be
arranged in Visio without link clutter. main.py reads it at the start of
every run (load) and rewrites it at the end (write_template) from that
run's final positions: nodes keep wherever they were left, new nodes
appear in the staging grid, and nodes no longer in the inventory drop out.

Each node's shape text is its name (a segment node's starts with
"segment "). Its Shape Data has a "Side" field: left, right, top or bottom
makes every link of the node leave from that side; empty leaves the choice
to the router.

Reading is plain zip and XML. Shapes are found by their text, so moving
them, grouping them, or saving the file in Visio keeps them readable. The
page records where its top-left corner sits in diagram pixels
(User.OriginX / User.OriginY, written by vsdx.py), so positions come back
exactly as they were written; a file without them reads with the page's
top-left corner at 0, 0.

Upgrading from draw.io: if the template doesn't exist yet but a
template.drawio sits next to it, positions are read from that once
(drawio_position_loader.py), and template.vsdx is written at the end of the
run. A --template path ending in .drawio is read the same way and the
template is written next to it as .vsdx.
"""
from __future__ import annotations

import io
import posixpath
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import diagram as dg
import inventory
import vsdx

SEGMENT_PREFIX = "segment "
SIDES = ("left", "right", "top", "bottom")
PAGE_ID = "positions"
SIDE_TIP = ("Move this node to place it in the diagram. Shape Data > Side: left, right, "
            "top or bottom makes every link of this node leave from that side.")

_V = "{http://schemas.microsoft.com/office/visio/2012/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PR = "{http://schemas.openxmlformats.org/package/2006/relationships}"


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def load(path, inventory_path) -> dict:
    """{name: {"x": centre x, "y": centre y, "side"?: side}} from the
    template, in diagram pixels. {} when there is no template yet.

    Raises ValueError for a shape whose text isn't an inventory host or a
    segment node, so a typo'd or stale name isn't silently left unpinned."""
    path = Path(path)
    source = path
    if not path.is_file():
        legacy = path.with_suffix(".drawio")
        if path.suffix.lower() != ".vsdx" or not legacy.is_file():
            return {}
        print(f"Reading node positions from {legacy} this once; "
              f"{path.name} is written at the end of this run.")
        source = legacy
    if source.suffix.lower() == ".drawio":
        import drawio_position_loader as dpl   # only for upgrading an old template
        return dpl.load_pinned_positions_from_drawio(source, inventory_path)

    hosts = set(inventory.host_vars(inventory_path))
    pinned, unknown = {}, []
    for name, cx, cy, data in read_shapes(source):
        if name not in hosts and not name.startswith(SEGMENT_PREFIX):
            unknown.append(name)
            continue
        entry = {"x": cx, "y": cy}
        side = (data.get("side") or "").strip().lower()
        if side in SIDES:
            entry["side"] = side
        elif side:
            print(f"positions: {name}: Side '{side}' isn't one of {', '.join(SIDES)}, ignored")
        pinned[name] = entry
    if unknown:
        raise ValueError(f"{source}: shape text(s) don't match any host in {inventory_path}: "
                         f"{sorted(set(unknown))}")
    unpinned = hosts - pinned.keys()
    if unpinned:
        print(f"No pinned position for {sorted(unpinned)} -- will use auto layout.")
    return pinned


def read_shapes(path) -> list:
    """[(text, centre x, centre y, {shape data label (lower case): value})]
    for every shape with text on the template's first page, in diagram
    pixels."""
    try:
        with zipfile.ZipFile(path) as z:
            page, content = _first_page(z)
    except (zipfile.BadZipFile, KeyError, StopIteration, ET.ParseError) as e:
        raise ValueError(f"{path}: not a readable Visio file ({e})") from None

    sheet = page.find(f"{_V}PageSheet")
    cells = _cells(sheet) if sheet is not None else {}
    page_h = _float(cells.get("PageHeight"), 0.0)
    user = {}
    if sheet is not None:
        for row in sheet.findall(f"{_V}Section[@N='User']/{_V}Row"):
            user[row.get("N")] = _float(_cells(row).get("Value"), 0.0)
    ox, oy = user.get("OriginX", 0.0), user.get("OriginY", 0.0)

    found = []
    shapes = content.find(f"{_V}Shapes")
    if shapes is not None:
        _walk(shapes, 0.0, 0.0, found)
    px = vsdx.PX_PER_INCH
    return [(text, round(cx * px + ox, 2), round((page_h - cy) * px + oy, 2), data)
            for text, cx, cy, data in found]


def _first_page(z: zipfile.ZipFile):
    pages = ET.fromstring(z.read("visio/pages/pages.xml"))
    page = pages.find(f"{_V}Page")
    if page is None:
        raise KeyError("no pages")
    rel_id = page.find(f"{_V}Rel").get(f"{_R}id")
    rels = ET.fromstring(z.read("visio/pages/_rels/pages.xml.rels"))
    target = next(r.get("Target") for r in rels.findall(f"{_PR}Relationship")
                  if r.get("Id") == rel_id)
    part = target.lstrip("/") if target.startswith("/") else \
        posixpath.normpath(posixpath.join("visio/pages", target))
    return page, ET.fromstring(z.read(part))


def _walk(shapes, left0, bottom0, out):
    """Collect shapes (and the shapes inside groups). left0, bottom0: where
    the parent's own coordinates start on the page, in inches."""
    for sh in shapes.findall(f"{_V}Shape"):
        c = _cells(sh)
        if "PinX" not in c or "PinY" not in c:
            continue
        w, h = _float(c.get("Width"), 0.0), _float(c.get("Height"), 0.0)
        left = left0 + _float(c["PinX"], 0.0) - _float(c.get("LocPinX"), w / 2)
        bottom = bottom0 + _float(c["PinY"], 0.0) - _float(c.get("LocPinY"), h / 2)
        inner = sh.find(f"{_V}Shapes")
        if inner is not None:
            _walk(inner, left, bottom, out)
        text_el = sh.find(f"{_V}Text")
        text = " ".join("".join(text_el.itertext()).split()) if text_el is not None else ""
        if not text:
            continue
        data = {}
        for row in sh.findall(f"{_V}Section[@N='Property']/{_V}Row"):
            rc = _cells(row)
            label = (rc.get("Label") or row.get("N") or "").strip().lower()
            data[label] = rc.get("Value") or ""
        out.append((text, left + w / 2, bottom + h / 2, data))


def _cells(el) -> dict:
    return {c.get("N"): c.get("V") for c in el.findall(f"{_V}Cell")}


def _float(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def output_path(path) -> Path:
    """Where the template is written: the given path, as .vsdx."""
    path = Path(path)
    return path.with_suffix(".vsdx") if path.suffix.lower() == ".drawio" else path


def template_diagram(nodes, sides: dict) -> dg.Diagram:
    """The template's one page: a copy of each node Shape (position, size,
    style, icon) with its name as text and its Side in Shape Data."""
    page = dg.Page(PAGE_ID, "Positions")
    for n in nodes:
        page.add(dg.Shape(n.id, style=dict(n.style), text=n.id, x=n.x, y=n.y, w=n.w, h=n.h,
                          icon=n.icon, tooltip=SIDE_TIP, data={"Side": sides.get(n.id, "")}))
    return dg.Diagram([page])


def write_template(path, nodes, sides: dict) -> Path:
    """Write the template for `nodes` (diagram.Shape) to path (as .vsdx).
    The file is only replaced once the new one is complete, so a failure
    leaves the old template, and its positions, as they were."""
    out = output_path(path)
    buf = io.BytesIO()
    vsdx.write(template_diagram(nodes, sides), buf)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_bytes(buf.getvalue())
    tmp.replace(out)
    return out
