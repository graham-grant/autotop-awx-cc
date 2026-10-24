"""
Converts routed Node/LinkSpec objects into draw.io XML.

build_drawio_xml() is moved here unchanged from drawio_hanan_router.py - it
emits nodes and edges with the exact polyline points the Hanan router
computed, plus exit/entry constraints and jumpStyle so crossings render
correctly. It knows nothing about this project's IP addresses, interface
names, or link status, since those aren't concerns of a generic router.

render() calls build_drawio_xml() to get that base XML, then layers on this
project's enhancements - an IP-address label and a rotated interface-id
label near each end of a link, and a green/red status dot at its midpoint
- as additional relative-position child cells of each edge. This is a
separate pass (parse the string back into a tree, add cells, re-serialize)
specifically so build_drawio_xml() itself stays exactly as provided rather
than being modified to know about project-specific details.

render_tree() returns the ElementTree root instead of a string, so main.py
can hand it to the feature plugins (features/) to add their own layers
before it's serialized.
"""
import xml.etree.ElementTree as ET

from config import (
    STATUS_UP_COLOR, STATUS_DOWN_COLOR, STATUS_DOT_SIZE,
    IP_LABEL_FRACTION, IP_LABEL_STYLE,
    INTF_LABEL_FRACTION, INTF_LABEL_STYLE, INTF_ABBREVIATIONS,
    BLANK_LABEL,
)
from pathfinder import Node, LinkSpec, Point
from icon_library import IconEntry


def abbreviate_intf(name):
    """'GigabitEthernet0/1' -> 'Gi0/1'. Already-short names (Junos/Arista
    style, e.g. 'ge-0/0/0' or 'Eth1') pass through unchanged.
    """
    if not name:
        return name
    for full, short in INTF_ABBREVIATIONS:
        if name.startswith(full):
            return short + name[len(full):]
    return name


# ---------------------------------------------------------------------------
# build_drawio_xml(): unchanged from drawio_hanan_router.py
# ---------------------------------------------------------------------------

def _exit_entry_attrs(side: str, frac: float) -> tuple[str, str]:
    if side == "left":
        return "0", str(frac)
    if side == "right":
        return "1", str(frac)
    if side == "top":
        return str(frac), "0"
    if side == "bottom":
        return str(frac), "1"
    raise ValueError(side)


def build_drawio_xml(nodes: list[Node], specs: list[LinkSpec]) -> str:
    mxfile = ET.Element("mxfile")
    diagram = ET.SubElement(mxfile, "diagram", name="Topology")
    model = ET.SubElement(diagram, "mxGraphModel")
    root = ET.SubElement(model, "root")
    ET.SubElement(root, "mxCell", id="0")
    ET.SubElement(root, "mxCell", id="1", parent="0")

    for n in nodes:
        cell = ET.SubElement(
            root, "mxCell", id=n.id, value=n.id,
            style="rounded=0;whiteSpace=wrap;html=1;", vertex="1", parent="1",
        )
        ET.SubElement(cell, "mxGeometry", x=str(n.x), y=str(n.y),
                      width=str(n.w), height=str(n.h), **{"as": "geometry"})

    for s in specs:
        ex_x, ex_y = _exit_entry_attrs(s.src_side, s.src_frac)
        en_x, en_y = _exit_entry_attrs(s.dst_side, s.dst_frac)
        style = (
            "edgeStyle=none;rounded=0;html=1;jumpStyle=arc;jumpSize=6;"
            f"exitX={ex_x};exitY={ex_y};exitDx=0;exitDy=0;"
            f"entryX={en_x};entryY={en_y};entryDx=0;entryDy=0;"
        )
        cell = ET.SubElement(root, "mxCell", id=s.id, style=style, edge="1",
                              source=s.src.id, target=s.dst.id, parent="1")
        geometry = ET.SubElement(cell, "mxGeometry", relative="1", **{"as": "geometry"})
        points_arr = ET.SubElement(geometry, "Array", **{"as": "points"})

        all_points: list[Point] = []
        for seg in s.segments:
            if not all_points:
                all_points.append(seg.p1)
            all_points.append(seg.p2)
        for pt in all_points[1:-1]:
            ET.SubElement(points_arr, "mxPoint", x=str(pt.x), y=str(pt.y))

    return ET.tostring(mxfile, encoding="unicode")


# ---------------------------------------------------------------------------
# Project-specific enhancements, layered on as a separate pass
# ---------------------------------------------------------------------------

def _apply_icon(cell, icon_entry: IconEntry):
    """Swap a node's plain-rectangle style for its preferred icon. The
    library style already bakes in verticalLabelPosition=bottom;
    verticalAlign=top, so the cell's existing value (the node id) ends up
    rendered as a caption under the icon rather than as text inside a box.
    Node geometry (x/y/w/h) is left untouched - it's still the fixed
    footprint pathfinder routes ports against - the icon just letterboxes
    inside it at its native aspect ratio.
    """
    cell.set("style", icon_entry.style)


def _add_status_dot(root, edge_id, color):
    """Small colored circle as a relative-position child of the edge,
    centered at its midpoint - stays on the actual rendered path no matter
    how many bends the Hanan router gave it.
    """
    dot = ET.SubElement(root, "mxCell", {
        "id": f"{edge_id}-status",
        "value": BLANK_LABEL,
        "style": f"ellipse;fillColor={color};strokeColor=#000000;",
        "vertex": "1", "connectable": "0", "parent": edge_id,
    })
    geom = ET.SubElement(dot, "mxGeometry", {
        "x": "0", "y": "0",
        "width": str(STATUS_DOT_SIZE), "height": str(STATUS_DOT_SIZE),
        "relative": "1", "as": "geometry",
    })
    ET.SubElement(geom, "mxPoint", {
        "x": str(-STATUS_DOT_SIZE / 2), "y": str(-STATUS_DOT_SIZE / 2), "as": "offset",
    })


def _add_edge_label(root, edge_id, text, x_fraction, suffix, style):
    label = ET.SubElement(root, "mxCell", {
        "id": f"{edge_id}-{suffix}",
        "value": text,
        "style": style,
        "vertex": "1", "connectable": "0", "parent": edge_id,
    })
    ET.SubElement(label, "mxGeometry", {
        "x": str(x_fraction), "relative": "1", "as": "geometry",
    })


def render(nodes, specs, edges_by_id, icons_by_id=None, icon_library=None):
    """render_tree(), serialized to a string."""
    return ET.tostring(render_tree(nodes, specs, edges_by_id, icons_by_id, icon_library),
                       encoding="unicode")


def render_tree(nodes, specs, edges_by_id, icons_by_id=None, icon_library=None):
    """Build the base XML via build_drawio_xml(), then add this project's
    IP/interface labels. Interface backgrounds change color based on link status.
    edges_by_id maps each LinkSpec's id to this project's edge dict.

    icons_by_id optionally maps each Node's id to a title in icon_library
    (see icon_library.load_icon_library) - if both are given and a node's
    id has a matching entry, that node's cell gets the icon style instead
    of the default rounded rectangle.
    """
    xml_str = build_drawio_xml(nodes, specs)
    mxfile = ET.fromstring(xml_str)
    root = mxfile.find(".//root")

    if icons_by_id and icon_library:
        node_ids = {n.id for n in nodes}
        for cell in root.findall("mxCell"):
            if cell.get("id") not in node_ids:
                continue
            icon_title = icons_by_id.get(cell.get("id"))
            if not icon_title:
                continue
            entry = icon_library.get(icon_title)
            if entry is None:
                print(f"render: no icon library entry for '{icon_title}' (node {cell.get('id')})")
                continue
            _apply_icon(cell, entry)

    for s in specs:
        edge = edges_by_id[s.id]

        # Determine color based on link state
        color = STATUS_UP_COLOR if edge["is_up"] else STATUS_DOWN_COLOR

        # --- IP Labels (White Backdrop) ---
        # ip_style = IP_LABEL_STYLE
        # if not ip_style.endswith(";"):
        #     ip_style += ";"
        # ip_style += "labelBackgroundColor=#ffffff;fontColor=#000000;opacity=70;"
        #
        # if edge.get("source_ip"):
        #     _add_edge_label(root, s.id, edge["source_ip"], -IP_LABEL_FRACTION, "ip-src", ip_style)
        # if edge.get("target_ip"):
        #     _add_edge_label(root, s.id, edge["target_ip"], IP_LABEL_FRACTION, "ip-tgt", ip_style)

        # --- Interface Labels (Status Colored Backdrop + Dynamic Rotation) ---
        base_intf = INTF_LABEL_STYLE
        if not base_intf.endswith(";"):
            base_intf += ";"

        # Defensively strip old rotation attributes
        base_intf = base_intf.replace("rotation=90;", "").replace("rotation=-90;", "").replace("horizontal=0;", "")

        # Inject the status color as the label background, with black text and border
        base_intf += f"labelBackgroundColor={color};fontColor=#000000;strokeColor=#000000;labelBorderColor=default;"

        # Apply rotation only if attached to top or bottom (-90 reads bottom-up)
        src_style = base_intf + ("rotation=-90;" if s.src_side in ("top", "bottom") else "")
        dst_style = base_intf + ("rotation=-90;" if s.dst_side in ("top", "bottom") else "")

        # Position at exactly -1.0 (source) and 1.0 (target) to pin directly against nodes
        _add_edge_label(root, s.id, abbreviate_intf(edge["source_intf"]),
                         -1.0, "intf-src", src_style)
        _add_edge_label(root, s.id, abbreviate_intf(edge["target_intf"]),
                         1.0, "intf-tgt", dst_style)

    return mxfile
