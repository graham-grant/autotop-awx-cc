"""
Draws the routed topology into a diagram.Diagram (format-independent; see
diagram.py). drawio.py and vsdx.py write it out.

build() makes one page, "Topology":
- one Shape per node at the position and size pathfinder settled on, with
  the node's icon where the inventory sets one, and the shared-segment
  style for segment nodes;
- one Line per link, through the exact waypoints the Hanan router chose,
  leaving and meeting each node at the side and fraction it picked (so
  draw.io keeps the link attached if a node is dragged). A link placed only
  by a MAC table is dashed, since it's inferred;
- a LineLabel at each end of each link with the interface name, coloured
  green or red by link status, rotated on top and bottom sides.

The feature plugins (features/) then add their own layers to the same page.
"""
from __future__ import annotations

import config
import diagram as dg
import icon_library
from config import STATUS_UP_COLOR, STATUS_DOWN_COLOR, INTF_ABBREVIATIONS

PAGE_ID = "topology"


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


def _side_fraction(side: str, frac: float) -> tuple:
    """(fx, fy) of a point on a node's side, as fractions of its size."""
    return {"left": (0.0, frac), "right": (1.0, frac),
            "top": (frac, 0.0), "bottom": (frac, 1.0)}[side]


def build(nodes: list, specs: list, edges_by_id: dict, icons_by_id=None, icons=None,
          segments=()) -> dg.Diagram:
    """The topology page. edges_by_id maps each LinkSpec's id to this
    project's edge dict; icons_by_id maps node ids to icon library titles
    and icons maps icon names to
    diagram.Icon (see icon_library.load); segments (from
    parser.extract_links) get the shared-segment style and their label
    (network, VRF, VLAN) instead of their id."""
    page = dg.Page(PAGE_ID, "Topology")
    seg_labels = {s["id"]: s["label"] for s in segments}
    icons_by_id = icons_by_id or {}

    for n in nodes:
        shape = dg.Shape(n.id, style=dict(config.NODE_STYLE), text=n.id,
                         x=n.x, y=n.y, w=n.w, h=n.h)
        if n.id in seg_labels:
            shape.style = dict(config.SEGMENT_NODE_STYLE)
            shape.text = seg_labels[n.id]
        title = icons_by_id.get(n.id)
        if title:
            shape.icon = icon_library.find(icons or {}, title)
            if shape.icon is None:
                print(f"render: no icon '{title}' in the icon folder (node {n.id})")
        page.add(shape)

    for s in specs:
        edge = edges_by_id[s.id]
        points = []
        for seg in s.segments:
            if not points:
                points.append((seg.p1.x, seg.p1.y))
            points.append((seg.p2.x, seg.p2.y))
        line_style = dict(config.LINK_STYLE)
        # A link placed by a MAC table, with no neighbor protocol to confirm
        # the ports, is inferred: draw it dashed
        evidence = edge.get("evidence") or []
        if "mac" in evidence and not {"lldp", "cdp"} & set(evidence):
            line_style.update(config.INFERRED_LINK_STYLE)
        page.add(dg.Line(s.id, style=line_style, source=s.src.id, target=s.dst.id,
                         waypoints=points[1:-1],
                         exit=_side_fraction(s.src_side, s.src_frac),
                         entry=_side_fraction(s.dst_side, s.dst_frac)))

        # Interface labels: status-coloured, pinned against each node (at
        # -1 and 1), turned to read bottom-up on top and bottom sides
        color = STATUS_UP_COLOR if edge["is_up"] else STATUS_DOWN_COLOR
        label_style = {**config.INTF_LABEL_STYLE, "label_bg": color}

        # Port-channels show their member count, "Po1 ×2", and an end whose
        # port-channel is split across switches says so: "Po1 MC-LAG"
        count = f" \u00d7{edge['members']}" if edge.get("members", 1) > 1 else ""
        mclag = edge.get("mclag") or []

        def intf_label(end):
            return (abbreviate_intf(edge[f"{end}_intf"]) + count
                    + (" MC-LAG" if end in mclag else ""))

        def turned(side):
            return {**label_style, "rotation": -90} if side in ("top", "bottom") else label_style

        page.add(dg.LineLabel(f"{s.id}-intf-src", style=turned(s.src_side),
                              text=intf_label("source"), line=s.id, at=-1.0))
        if edge["target_intf"]:  # links to a segment node have no far interface
            page.add(dg.LineLabel(f"{s.id}-intf-tgt", style=turned(s.dst_side),
                                  text=intf_label("target"), line=s.id, at=1.0))

    return dg.Diagram([page])
