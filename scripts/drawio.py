"""
draw.io writer: turns a diagram.Diagram into a draw.io document
(<mxfile>). vsdx.py is the separate Visio writer of the same Diagram;
neither reads the other's output.

  Diagram / Page        <mxfile> / one <diagram> per page
  Layer                 a layer cell (child of the root cell); base-layer
                        items sit on the default layer, cell "1"
  Shape                 a vertex: rectangle, ellipse, text, or an image
                        (Icon) with its name below
  Line                  an edge between the two shapes' cells, with its
                        waypoints and exit/entry constraints, so draw.io
                        keeps it attached when a node is dragged
  LineLabel             a vertex that is a child of its edge, at `at` along
                        it (draw.io's own relative geometry)
  tooltip, link, tags,  an <object> wrapping the cell. A "page:<id>" link
  data                  becomes data:page/id,<id>; one with highlight tags
                        becomes a data:action/json "open" + "highlight"

Labels are written as HTML (html=1), so text is escaped and line breaks
become <br>.
"""
from __future__ import annotations

import base64
import html
import json
import xml.etree.ElementTree as ET

import diagram as dg

# A plain "" label on a draw.io vertex falls back to showing the cell's id.
# U+200B isn't treated as whitespace, so it renders as an invisible label.
BLANK_LABEL = "​"


def _num(v) -> str:
    v = float(v)
    return str(int(v)) if v == int(v) else f"{v:.2f}".rstrip("0").rstrip(".")


def _label(text: str) -> str:
    return html.escape(text, quote=False).replace("\n", "<br>")


def _link(link: str | None) -> str | None:
    parsed = dg.parse_link(link)
    if parsed is None:
        return link
    kind, target, tags = parsed
    if kind == "url":
        return target
    if not tags:
        return f"data:page/id,{target}"
    actions = {"actions": [{"open": f"data:page/id,{target}"},
                           {"highlight": {"tags": tags}}]}
    return "data:action/json," + json.dumps(actions, separators=(",", ":"))


def style_string(item) -> str:
    """The draw.io style for an item's neutral style dict."""
    st = item.style
    kind = st.get("kind", "box")
    out = []
    if isinstance(item, dg.Line):
        out.append("edgeStyle=none")
        if st.get("curved"):
            out.append("curved=1")
        out += ["rounded=0", "html=1"]
        out += ["jumpStyle=arc", "jumpSize=6"] if st.get("jumps") else ["jumpStyle=none"]
        if "end_arrow" in st:
            out.append(f"endArrow={st['end_arrow']}")
        if "start_arrow" in st:
            out.append(f"startArrow={st['start_arrow']}")
    else:
        icon = getattr(item, "icon", None)
        if icon is not None:
            out += ["shape=image", "html=1"]
            if icon.label_below:
                out += ["verticalLabelPosition=bottom", "verticalAlign=top"]
            payload = base64.b64encode(icon.data).decode()
            out += ["aspect=fixed", f"image=data:{icon.mime},{payload}"]
            return ";".join(out) + ";"
        if kind == "text":
            out.append("text")
        elif kind == "ellipse":
            out.append("ellipse")
        if kind != "ellipse":
            out.append(f"rounded={1 if st.get('rounded') else 0}")
            if st.get("rounded") and "arc" in st:
                out.append(f"arcSize={_num(st['arc'])}")
        if st.get("wrap"):
            out.append("whiteSpace=wrap")
        out.append("html=1")
        if "align" in st or kind == "text":
            out.append(f"align={st.get('align', 'center')}")
        if "valign" in st or kind == "text":
            out.append(f"verticalAlign={st.get('valign', 'middle')}")
    keys = [("font_size", "fontSize"), ("fill", "fillColor"), ("stroke", "strokeColor"),
            ("font_color", "fontColor"), ("label_bg", "labelBackgroundColor"),
            ("label_border", "labelBorderColor"), ("spacing", "spacing"),
            ("stroke_width", "strokeWidth"), ("rotation", "rotation")]
    for key, name in keys:
        if key in st:
            value = st[key]
            out.append(f"{name}={_num(value) if isinstance(value, (int, float)) else value}")
    if st.get("bold"):
        out.append("fontStyle=1")
    if st.get("dashed"):
        out.append("dashed=1")
        if st.get("dash"):
            out.append(f"dashPattern={st['dash']}")
    return ";".join(out) + ";"


def _cell(item, parent: str, attrs: dict) -> ET.Element:
    """The item's mxCell, wrapped in an <object> when it carries a tooltip,
    link, tags or data. Returns the outer element."""
    text = _label(item.text)
    wrapped = item.tooltip or item.link or item.tags or item.data
    cell_attrs = {"style": style_string(item), **attrs, "parent": parent}
    if wrapped:
        outer = ET.Element("object", {"id": item.id, "label": text})
        if item.tooltip:
            outer.set("tooltip", item.tooltip)
        if item.link:
            outer.set("link", _link(item.link))
        if item.tags:
            outer.set("tags", " ".join(item.tags))
        for k, v in item.data.items():
            outer.set(k, str(v))
        ET.SubElement(outer, "mxCell", cell_attrs)
        return outer
    return ET.Element("mxCell", {"id": item.id, "value": text, **cell_attrs})


def _inner(outer: ET.Element) -> ET.Element:
    return outer if outer.tag == "mxCell" else outer.find("mxCell")


def page_root(page: dg.Page) -> ET.Element:
    """One page's <root>: cells 0 and 1, the base layer's items, then each
    layer's cell followed by its items (draw.io stacks layers in order)."""
    root = ET.Element("root")
    ET.SubElement(root, "mxCell", id="0")
    ET.SubElement(root, "mxCell", id="1", parent="0")
    lines = {i.id: i for i in page.items if isinstance(i, dg.Line)}

    def layer_of(item):
        if isinstance(item, dg.LineLabel) and item.layer is None and item.line in lines:
            return lines[item.line].layer
        return item.layer

    groups = [(None, [i for i in page.items if layer_of(i) is None])]
    groups += [(lay, [i for i in page.items if layer_of(i) == lay.id]) for lay in page.layers]
    for lay, items in groups:
        parent = "1"
        if lay is not None:
            attrs = {"id": lay.id, "value": lay.name, "parent": "0"}
            if lay.locked:
                attrs["style"] = "locked=1;"
            if not lay.visible:
                attrs["visible"] = "0"
            ET.SubElement(root, "mxCell", attrs)
            parent = lay.id
        for item in items:
            root.append(_element(item, parent))
    return root


def _element(item, parent: str) -> ET.Element:
    if isinstance(item, dg.Line):
        outer = _cell(item, parent, {"edge": "1", "source": item.source, "target": item.target})
        cell = _inner(outer)
        st = cell.get("style")
        if item.exit:
            st += (f"exitX={_num(item.exit[0])};exitY={_num(item.exit[1])};exitDx=0;exitDy=0;"
                   "exitPerimeter=0;")
        if item.entry:
            st += (f"entryX={_num(item.entry[0])};entryY={_num(item.entry[1])};entryDx=0;"
                   "entryDy=0;entryPerimeter=0;")
        cell.set("style", st)
        geom = ET.SubElement(cell, "mxGeometry", {"relative": "1", "as": "geometry"})
        if item.waypoints:
            arr = ET.SubElement(geom, "Array", {"as": "points"})
            for x, y in item.waypoints:
                ET.SubElement(arr, "mxPoint", {"x": _num(x), "y": _num(y)})
        return outer
    if isinstance(item, dg.LineLabel):
        outer = _cell(item, item.line, {"vertex": "1", "connectable": "0"})
        geom_attrs = {"x": _num(item.at), "relative": "1", "as": "geometry"}
        if item.w and item.h:
            geom_attrs.update(y="0", width=_num(item.w), height=_num(item.h))
        geom = ET.SubElement(_inner(outer), "mxGeometry", geom_attrs)
        if item.offset != (0.0, 0.0) and item.offset != (0, 0):
            ET.SubElement(geom, "mxPoint", {"x": _num(item.offset[0]), "y": _num(item.offset[1]),
                                            "as": "offset"})
        _blank(outer, item)
        return outer
    outer = _cell(item, parent, {"vertex": "1"} if item.layer is None
                  else {"vertex": "1", "connectable": "0"})
    ET.SubElement(_inner(outer), "mxGeometry", {"x": _num(item.x), "y": _num(item.y),
                                                "width": _num(item.w), "height": _num(item.h),
                                                "as": "geometry"})
    _blank(outer, item)
    return outer


def _blank(outer, item):
    if not item.text:
        outer.set("label" if outer.tag == "object" else "value", BLANK_LABEL)


def to_mxfile(diagram: dg.Diagram) -> ET.Element:
    mxfile = ET.Element("mxfile")
    for page in diagram.pages:
        d = ET.SubElement(mxfile, "diagram", id=page.id, name=page.name)
        model = ET.SubElement(d, "mxGraphModel")
        model.append(page_root(page))
    return mxfile


def write(diagram: dg.Diagram, path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(ET.tostring(to_mxfile(diagram), encoding="unicode"))
