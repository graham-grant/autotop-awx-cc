"""The draw.io writer (scripts/drawio.py) and the diagram model
(scripts/diagram.py): how each kind of item is written for draw.io, and
the model's own geometry, which the Visio writer uses."""
import json
import unittest
import xml.etree.ElementTree as ET

import support  # noqa: F401
import diagram as dg
import drawio


def one_page(items, layers=()):
    p = dg.Page("p1", "Topology", layers=list(layers))
    for item in items:
        p.add(item)
    return dg.Diagram([p])


class DrawioWriterTest(unittest.TestCase):
    def setUp(self):
        lay = dg.Layer("layer-x", "X", visible=False, locked=True)
        items = [
            dg.Shape("a", x=0, y=0, w=96, h=48, text="A & B", style={"wrap": True}),
            dg.Shape("b", x=300, y=200, w=96, h=48, text="B",
                     style={"rounded": True, "arc": 50, "fill": "#f5f5f5"}),
            dg.Line("l", source="a", target="b", waypoints=[(200, 24)], exit=(1, 0.5),
                    entry=(0, 0.5), style={"jumps": True, "dashed": True, "dash": "4 4"}),
            dg.LineLabel("l-src", line="l", at=-1, text="Gi1",
                         style={"kind": "text", "label_bg": "#00CC00", "rotation": -90}),
            dg.Shape("t", x=0, y=300, w=60, h=14, text="tag", layer="layer-x",
                     style={"kind": "text"}, tooltip="line 1\nline 2",
                     link="page:p2#highlight=sn-1", tags=("sn-1",), data={"vrf": "blue"}),
        ]
        self.root = drawio.to_mxfile(one_page(items, [lay])).find("diagram/mxGraphModel/root")
        self.by_id = {el.get("id"): el for el in self.root}

    def test_cells_and_layers(self):
        self.assertEqual([el.get("id") for el in self.root],
                         ["0", "1", "a", "b", "l", "l-src", "layer-x", "t"])
        layer = self.by_id["layer-x"]
        self.assertEqual((layer.get("parent"), layer.get("value"), layer.get("visible"),
                          layer.get("style")), ("0", "X", "0", "locked=1;"))
        self.assertEqual(self.by_id["t"].find("mxCell").get("parent"), "layer-x")
        self.assertEqual(self.by_id["l-src"].get("parent"), "l")

    def test_styles(self):
        self.assertEqual(self.by_id["a"].get("style"), "rounded=0;whiteSpace=wrap;html=1;")
        self.assertIn("arcSize=50;", self.by_id["b"].get("style"))
        line = self.by_id["l"].get("style")
        for part in ("edgeStyle=none;", "jumpStyle=arc;", "dashed=1;", "dashPattern=4 4;",
                     "exitX=1;exitY=0.5;", "entryX=0;entryY=0.5;"):
            self.assertIn(part, line)
        label = self.by_id["l-src"].get("style")
        self.assertTrue(label.startswith("text;"))
        self.assertIn("labelBackgroundColor=#00CC00;", label)
        self.assertIn("rotation=-90;", label)

    def test_geometry(self):
        pts = self.by_id["l"].findall("mxGeometry/Array/mxPoint")
        self.assertEqual([(p.get("x"), p.get("y")) for p in pts], [("200", "24")])
        geo = self.by_id["l-src"].find("mxGeometry")
        self.assertEqual((geo.get("x"), geo.get("relative")), ("-1", "1"))

    def test_object_wrapper(self):
        obj = self.by_id["t"]
        self.assertEqual(obj.tag, "object")
        self.assertEqual(obj.get("tooltip"), "line 1\nline 2")
        self.assertEqual((obj.get("tags"), obj.get("vrf")), ("sn-1", "blue"))
        action = json.loads(obj.get("link")[len("data:action/json,"):])
        self.assertEqual(action["actions"], [{"open": "data:page/id,p2"},
                                             {"highlight": {"tags": ["sn-1"]}}])

    def test_label_is_escaped_html(self):
        self.assertEqual(self.by_id["a"].get("value"), "A &amp; B")

    def test_icon(self):
        pic = dg.Icon(b"\x89PNG\r\n\x1a\n....", "image/png", label_below=True)
        root = drawio.to_mxfile(one_page([dg.Shape("s", w=10, h=10, text="s", icon=pic)]))
        style = root.find(".//mxCell[@id='s']").get("style")
        self.assertTrue(style.startswith("shape=image;"))
        self.assertIn("verticalLabelPosition=bottom;", style)
        self.assertIn("image=data:image/png,iVBORw0KGgouLi4u;", style)

    def test_serialises(self):
        ET.fromstring(ET.tostring(drawio.to_mxfile(one_page([]))))


class ModelTest(unittest.TestCase):
    def test_unknown_style_key_is_refused(self):
        with self.assertRaises(ValueError):
            dg.Shape("s", style={"colour": "#000000"})

    def test_duplicate_ids_are_refused(self):
        p = dg.Page("p", "P")
        p.add(dg.Shape("s"))
        with self.assertRaises(ValueError):
            p.add(dg.Shape("s"))

    def test_route_with_and_without_constraints(self):
        p = one_page([dg.Shape("a", x=0, y=0, w=100, h=100), dg.Shape("b", x=300, y=0, w=100, h=100),
                      dg.Line("fixed", source="a", target="b", exit=(1, 0.25), entry=(0, 0.75)),
                      dg.Line("free", source="a", target="b")]).pages[0]
        self.assertEqual(p.route(p.get("fixed")), [(100, 25), (300, 75)])
        self.assertEqual(p.route(p.get("free")), [(100, 50), (300, 50)])

    def test_label_point(self):
        p = one_page([dg.Shape("a", w=10, h=10), dg.Shape("b", x=110, w=10, h=10),
                      dg.Line("l", source="a", target="b", exit=(1, 0), entry=(0, 0)),
                      dg.LineLabel("m", line="l", at=0, offset=(0, 5))]).pages[0]
        self.assertEqual(p.label_point(p.get("m")), (60, 5))

    def test_links(self):
        self.assertEqual(dg.parse_link("page:p2#highlight=a,b"), ("page", "p2", ["a", "b"]))
        self.assertEqual(dg.parse_link("https://x"), ("url", "https://x", []))
        self.assertIsNone(dg.parse_link("ftp://x"))


if __name__ == "__main__":
    unittest.main()
