"""
Load pinned node positions from a .drawio file instead of layout.yml.

Drop-in replacement for load_pinned_positions(layout_path): same return
shape -- {hostname: {"x": ..., "y": ..., "side": ...}} with "side" only
present if a node actually has one set -- so existing call sites work
unchanged:

    centers[name] = (pinned[name]["x"], pinned[name]["y"])

Every node label found in the diagram must match a host in inventory.yml,
except shared-segment nodes, whose labels start with "segment " (they come
from the facts, not the inventory, and a stale one is simply unused).
Hosts in the inventory with no matching node in the diagram are left
unpinned (same opt-in semantics as before) rather than treated as errors.
"""

from __future__ import annotations
import html
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import inventory


def _collect_inventory_hosts(inventory_path: str | Path) -> set[str]:
    """Every hostname under any 'hosts:' key, however deeply nested."""
    return set(inventory.host_vars(inventory_path))


def _label_text(raw: str | None) -> str:
    """draw.io labels are frequently html=1 -- entities and stray <br>/<div>
    wrapping included even for plain-looking text. Strip that down to the
    bare hostname."""
    text = html.unescape(raw or "")
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


_RESERVED_OBJECT_ATTRS = {"id", "label"}
SEGMENT_PREFIX = "segment "


def load_pinned_positions_from_drawio(drawio_path: str | Path, inventory_path: str | Path) -> dict:
    """Load {hostname: {x, y, side?}} from vertex positions in a .drawio
    file. Returns {} if drawio_path doesn't exist -- pinning stays
    opt-in, matching load_pinned_positions's behaviour for layout.yml.

    Raises ValueError if any node label in the diagram doesn't match a
    host in inventory.yml -- catches a typo'd or stale node name in the
    diagram rather than silently leaving it unpinned or mis-keyed.
    """
    drawio_path = Path(drawio_path)
    if not drawio_path.is_file():
        return {}

    inventory_hosts = _collect_inventory_hosts(inventory_path)

    tree = ET.parse(drawio_path)
    graph_root = tree.getroot().find(".//root")
    if graph_root is None:
        raise ValueError(f"{drawio_path}: no <root> element found -- not a recognizable draw.io file")

    pinned: dict[str, dict] = {}
    unknown: list[str] = []

    for child in graph_root:
        if child.tag == "mxCell" and child.get("vertex") == "1":
            cell, label, extra = child, child.get("value"), {}
        elif child.tag in ("object", "UserObject"):
            cell = child.find("mxCell")
            if cell is None or cell.get("vertex") != "1":
                continue
            label = child.get("label")
            extra = {k: v for k, v in child.attrib.items() if k not in _RESERVED_OBJECT_ATTRS}
        else:
            continue

        geometry = cell.find("mxGeometry")
        if geometry is None:
            continue

        name = _label_text(label)
        if not name:
            continue

        if name not in inventory_hosts and not name.startswith(SEGMENT_PREFIX):
            unknown.append(name)
            continue

        x = float(geometry.get("x", 0))
        y = float(geometry.get("y", 0))
        w = float(geometry.get("width", 0))
        h = float(geometry.get("height", 0))

        entry = {"x": x + w / 2, "y": y + h / 2}
        if "side" in extra:
            entry["side"] = extra["side"]
        pinned[name] = entry

    if unknown:
        raise ValueError(
            f"{drawio_path}: node label(s) don't match any host in {inventory_path}: "
            f"{sorted(set(unknown))}"
        )

    unpinned = inventory_hosts - pinned.keys()  # segments aren't reported
    if unpinned:
        print(f"No pinned position for {sorted(unpinned)} -- will use auto layout.")

    return pinned


_TEMPLATE_NODE_STYLE = "rounded=0;whiteSpace=wrap;html=1;"


def build_template_xml(positions: dict, sides: dict | None = None) -> str:
    """Build a minimal .drawio file containing only one rectangle per node
    -- no links, no IP/interface labels, no status dots -- so repositioning
    nodes by hand isn't fighting through link clutter.

    positions: {hostname: (x, y, w, h)} (top-left corner, draw.io convention)
    sides: optional {hostname: side} for any node with a preferred
    attachment side set -- round-tripped via the same custom "side"
    property load_pinned_positions_from_drawio() already knows to read
    back off an <object>-wrapped vertex.

    Returns the XML string; write it to template.drawio yourself.
    """
    sides = sides or {}
    mxfile = ET.Element("mxfile")
    diagram = ET.SubElement(mxfile, "diagram", name="Positions")
    model = ET.SubElement(diagram, "mxGraphModel")
    root = ET.SubElement(model, "root")
    ET.SubElement(root, "mxCell", id="0")
    ET.SubElement(root, "mxCell", id="1", parent="0")

    for i, name in enumerate(sorted(positions)):
        x, y, w, h = positions[name]
        cell_id = str(i + 2)
        if name in sides:
            obj = ET.SubElement(root, "object", id=cell_id, label=name, side=sides[name])
            cell = ET.SubElement(obj, "mxCell", style=_TEMPLATE_NODE_STYLE, vertex="1", parent="1")
        else:
            cell = ET.SubElement(root, "mxCell", id=cell_id, value=name,
                                  style=_TEMPLATE_NODE_STYLE, vertex="1", parent="1")
        ET.SubElement(cell, "mxGeometry", x=str(x), y=str(y), width=str(w), height=str(h),
                      **{"as": "geometry"})

    return ET.tostring(mxfile, encoding="unicode")


# ---------------------------------------------------------------------------
# Self-contained demo (doesn't depend on any real inventory/drawio files)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    sample_inventory = """
all:
  children:
    network_devices:
      hosts:
        9200-1: {}
        9200-2: {}
        9200-3: {}
"""

    # 9200-1: plain value label.
    # 9200-2: wrapped in <object> with a custom "side" attribute, the way
    #         draw.io stores it once you add a custom property via Edit Data.
    sample_drawio = """<mxfile>
  <diagram>
    <mxGraphModel>
      <root>
        <mxCell id="0"/>
        <mxCell id="1" parent="0"/>
        <mxCell id="2" value="9200-1" vertex="1" parent="1">
          <mxGeometry x="40" y="130" width="120" height="60" as="geometry"/>
        </mxCell>
        <object label="9200-2" side="left" id="3">
          <mxCell style="rounded=0;" vertex="1" parent="1">
            <mxGeometry x="400" y="130" width="120" height="60" as="geometry"/>
          </mxCell>
        </object>
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>"""

    with tempfile.TemporaryDirectory() as tmp:
        inv_path = Path(tmp) / "inventory.yml"
        drawio_path = Path(tmp) / "topology.drawio"
        inv_path.write_text(sample_inventory)
        drawio_path.write_text(sample_drawio)

        pinned = load_pinned_positions_from_drawio(drawio_path, inv_path)
        print("pinned:", pinned)
        # 9200-3 is in the inventory but has no node in the diagram -- left
        # unpinned, with the printed note above, not an error.
