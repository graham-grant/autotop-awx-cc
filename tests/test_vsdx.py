"""Visio output (scripts/vsdx.py): the package is a valid OPC zip whose
parts and relationships all resolve, shapes land where draw.io puts them
(inches, y flipped), layers, tooltips, page links and data carry over,
icons become pictures, and main.py writes topology.vsdx beside
topology.drawio. If LibreOffice is installed, it must open the file."""
import base64
import io
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
import zlib
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import support
import config
import icon_library
import main
import vsdx

V = "{%s}" % vsdx.NS
PKG = "{%s}" % vsdx.PKG_REL_NS
CT = "{http://schemas.openxmlformats.org/package/2006/content-types}"


def mxfile(*pages):
    """pages: (id, name, [cell xml strings]) -> mxfile element."""
    out = "<mxfile>"
    for pid, name, cells in pages:
        out += (f'<diagram id="{pid}" name="{name}"><mxGraphModel><root>'
                '<mxCell id="0"/><mxCell id="1" parent="0"/>' + "".join(cells)
                + "</root></mxGraphModel></diagram>")
    return ET.fromstring(out + "</mxfile>")


def box(cid, x, y, w, h, value="", style="rounded=0;whiteSpace=wrap;html=1;", parent="1"):
    return (f'<mxCell id="{cid}" value="{value}" style="{style}" vertex="1" parent="{parent}">'
            f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')


class Package:
    """A written .vsdx, opened for checking."""

    def __init__(self, root):
        buf = io.BytesIO()
        self.counts = vsdx.write(root, buf)
        self.zip = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
        self.names = set(self.zip.namelist())

    def xml(self, name):
        return ET.fromstring(self.zip.read(name))

    def page(self, n=1):
        return self.xml(f"visio/pages/page{n}.xml")

    def shapes(self, n=1):
        return self.page(n).findall(f"{V}Shapes/{V}Shape")

    @staticmethod
    def cell(shape, name):
        el = shape.find(f"{V}Cell[@N='{name}']")
        return None if el is None else el.get("V")

    def num(self, shape, name):
        return float(self.cell(shape, name))

    @staticmethod
    def text(shape):
        el = shape.find(f"{V}Text")
        return None if el is None else el.text


class PackageTest(unittest.TestCase):
    def setUp(self):
        self.pkg = Package(mxfile(("p1", "Topology", [box("a", 0, 0, 96, 48, "A")]),
                                  ("p2", "Routes · A", [box("b", 0, 0, 96, 48, "B")])))

    def test_every_part_is_xml_and_every_relationship_resolves(self):
        z = self.pkg
        for name in z.names:
            if name.endswith((".xml", ".rels")):
                z.xml(name)   # parses
        for rels in [n for n in z.names if n.endswith(".rels")]:
            base = rels.replace("_rels/", "").rsplit(".rels", 1)[0]
            folder = base.rsplit("/", 1)[0] + "/" if "/" in base else ""
            for rel in z.xml(rels).findall(f"{PKG}Relationship"):
                target = rel.get("Target")
                path = Path(folder + target).as_posix()
                while "/../" in path or path.startswith("../"):
                    path = re.sub(r"[^/]+/\.\./", "", path, count=1)
                self.assertIn(path, z.names, f"{rels} -> {target}")

    def test_content_types_cover_each_part(self):
        types = self.pkg.xml("[Content_Types].xml")
        overrides = {o.get("PartName") for o in types.findall(f"{CT}Override")}
        self.assertTrue({"/visio/document.xml", "/visio/pages/pages.xml",
                         "/visio/pages/page1.xml", "/visio/pages/page2.xml"} <= overrides)
        for part in overrides:
            self.assertIn(part.lstrip("/"), self.pkg.names)

    def test_pages_keep_their_names_and_order(self):
        pages = self.pkg.xml("visio/pages/pages.xml").findall(f"{V}Page")
        self.assertEqual([p.get("Name") for p in pages], ["Topology", "Routes · A"])
        self.assertEqual(self.pkg.counts, {"pages": 2, "shapes": 2, "images": 0})

    def test_duplicate_page_names_are_made_unique(self):
        pkg = Package(mxfile(("p1", "Page", []), ("p2", "Page", [])))
        names = [p.get("Name") for p in pkg.xml("visio/pages/pages.xml").findall(f"{V}Page")]
        self.assertEqual(names, ["Page", "Page (2)"])

    def test_compressed_page_is_read(self):
        model = ('<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
                 + box("a", 0, 0, 96, 48, "zipped") + "</root></mxGraphModel>")
        comp = zlib.compressobj(9, zlib.DEFLATED, -15)
        data = comp.compress(urllib.parse.quote(model).encode()) + comp.flush()
        root = ET.fromstring(f'<mxfile><diagram id="p" name="P">'
                             f'{base64.b64encode(data).decode()}</diagram></mxfile>')
        (shape,) = Package(root).shapes()
        self.assertEqual(Package.text(shape), "zipped")


class GeometryTest(unittest.TestCase):
    def test_inches_and_y_flipped(self):
        # two boxes 2 in apart; the page is their bounds plus a 0.5 in margin
        pkg = Package(mxfile(("p", "P", [box("a", 100, 100, 96, 48, "top"),
                                          box("b", 100, 292, 192, 96, "low")])))
        a, b = pkg.shapes()
        sheet = pkg.xml("visio/pages/pages.xml").find(f"{V}Page/{V}PageSheet")
        self.assertAlmostEqual(pkg.num(sheet, "PageWidth"), 2 + 1)
        self.assertAlmostEqual(pkg.num(sheet, "PageHeight"), (388 - 100) / 96 + 1)
        self.assertAlmostEqual(pkg.num(a, "Width"), 1)
        self.assertAlmostEqual(pkg.num(a, "PinX"), 0.5 + 0.5)
        self.assertGreater(pkg.num(a, "PinY"), pkg.num(b, "PinY"))   # y points up
        self.assertAlmostEqual(pkg.num(b, "PinY"), 0.5 + 0.5)

    def test_rotation_ellipse_and_rounding(self):
        pkg = Package(mxfile(("p", "P", [
            box("r", 0, 0, 40, 20, "Gi1", "text;html=1;rotation=-90;labelBackgroundColor=#00CC00;"
                "strokeColor=#000000;labelBorderColor=default;"),
            box("e", 100, 0, 14, 14, "", "ellipse;fillColor=#ff0000;"),
            box("p", 200, 0, 100, 40, "pill", "rounded=1;arcSize=50;")])))
        rot, dot, pill = pkg.shapes()
        self.assertAlmostEqual(pkg.num(rot, "Angle"), math.pi / 2, places=5)
        self.assertEqual(pkg.cell(rot, "FillForegnd"), "#00CC00")
        self.assertEqual(pkg.cell(rot, "LinePattern"), "1")
        self.assertEqual(pkg.cell(dot, "FillForegnd"), "#FF0000")
        self.assertIsNotNone(dot.find(f"{V}Section[@N='Geometry']/{V}Row[@T='Ellipse']"))
        arcs = pill.findall(f"{V}Section[@N='Geometry']/{V}Row[@T='ArcTo']")
        self.assertEqual(len(arcs), 4)   # corner radius = half the height: a pill

    def test_edge_follows_routed_points_and_label_sits_on_it(self):
        pkg = Package(mxfile(("p", "P", [
            box("a", 0, 0, 96, 96, "A"), box("b", 288, 192, 96, 96, "B"),
            '<mxCell id="e" style="endArrow=none;exitX=1;exitY=0.5;exitDx=0;exitDy=0;'
            'entryX=0.5;entryY=0;entryDx=0;entryDy=0;dashed=1;" edge="1" parent="1" source="a" '
            'target="b"><mxGeometry relative="1" as="geometry"><Array as="points">'
            '<mxPoint x="336" y="48"/></Array></mxGeometry></mxCell>',
            '<mxCell id="l" value="mid" style="text;html=1;" vertex="1" parent="e">'
            '<mxGeometry x="0" relative="1" as="geometry"><mxPoint as="offset"/></mxGeometry></mxCell>'])))
        _, _, line, label = pkg.shapes()
        rows = line.findall(f"{V}Section[@N='Geometry']/{V}Row")
        left, bottom = (pkg.num(line, "PinX") - pkg.num(line, "Width") / 2,
                        pkg.num(line, "PinY") - pkg.num(line, "Height") / 2)
        pts = [(round(left + float(Package.cell(r, "X")), 4),
                round(bottom + float(Package.cell(r, "Y")), 4)) for r in rows]
        page_h = (288 - 0) / 96 + 1
        want = [(96, 48), (336, 48), (336, 192)]   # exit, waypoint, entry
        self.assertEqual(pts, [(round(x / 96 + 0.5, 4), round(page_h - (y / 96 + 0.5), 4))
                               for x, y in want])
        self.assertEqual((pkg.cell(line, "EndArrow"), pkg.cell(line, "LinePattern")), ("0", "2"))
        # half way along the 240 + 144 unit line is 192 units in: (288, 48)
        self.assertAlmostEqual(pkg.num(label, "PinX"), 288 / 96 + 0.5)
        self.assertAlmostEqual(pkg.num(label, "PinY"), page_h - (48 / 96 + 0.5))

    def test_default_arrow_and_curves(self):
        cells = [box("a", 0, 0, 96, 96), box("b", 400, 400, 96, 96),
                 '<mxCell id="e1" style="" edge="1" parent="1" source="a" target="b">'
                 '<mxGeometry relative="1" as="geometry"/></mxCell>',
                 '<mxCell id="e2" style="curved=1;endArrow=none;" edge="1" parent="1" source="a" '
                 'target="b"><mxGeometry relative="1" as="geometry"><Array as="points">'
                 '<mxPoint x="300" y="40"/></Array></mxGeometry></mxCell>']
        pkg = Package(mxfile(("p", "P", cells)))
        straight, curved = pkg.shapes()[2:]
        self.assertEqual(pkg.cell(straight, "EndArrow"), str(vsdx._ARROWS["classic"]))
        self.assertGreater(len(curved.findall(f"{V}Section[@N='Geometry']/{V}Row")), 8)


class DataTest(unittest.TestCase):
    def setUp(self):
        cells = [
            '<mxCell id="layer-vrf" value="VRF · blue" style="locked=1;" parent="0" visible="0"/>',
            box("a", 0, 0, 96, 48, "A"),
            '<object id="dot" label="&lt;b&gt;1&lt;/b&gt;" tooltip="10.0.0.0/24&#10;via ospf" '
            'link="data:action/json,{&quot;actions&quot;:[{&quot;open&quot;:&quot;data:page/id,'
            'p2&quot;},{&quot;highlight&quot;:{&quot;tags&quot;:[&quot;sn-1&quot;]}}]}" '
            'tags="vrf-blue sn-1" vrf="blue" next-hop="x">'
            + box("dot", 0, 100, 14, 14, style="ellipse;html=1;", parent="layer-vrf")
            .replace(' id="dot" value=""', "").replace("<mxCell", "<mxCell", 1)
            + "</object>",
            '<object id="w" label="web" link="https://example.com/x">'
            + box("w", 200, 0, 96, 48, parent="1").replace(' id="w" value=""', "") + "</object>",
        ]
        self.pkg = Package(mxfile(("p1", "Topology", cells), ("p2", "Subnets", [])))
        self.shapes = {Package.text(s): s for s in self.pkg.shapes()}

    def test_layers(self):
        rows = self.pkg.xml("visio/pages/pages.xml").find(f"{V}Page").findall(
            f"{V}PageSheet/{V}Section[@N='Layer']/{V}Row")
        got = [(Package.cell(r, "Name"), Package.cell(r, "Visible"), Package.cell(r, "Lock"))
               for r in rows]
        self.assertEqual(got, [("Background", "1", "0"), ("VRF · blue", "0", "1")])
        self.assertEqual(Package.cell(self.shapes["A"], "LayerMember"), "0")
        self.assertEqual(Package.cell(self.shapes["1"], "LayerMember"), "1")

    def test_tooltip_link_and_data(self):
        dot = self.shapes["1"]
        self.assertEqual(Package.cell(dot, "Comment"), "10.0.0.0/24\nvia ospf")
        link = dot.find(f"{V}Section[@N='Hyperlink']/{V}Row")
        self.assertEqual(Package.cell(link, "SubAddress"), "Subnets")   # the action's "open"
        props = {Package.cell(r, "Label"): (r.get("N"), Package.cell(r, "Value"))
                 for r in dot.findall(f"{V}Section[@N='Property']/{V}Row")}
        self.assertEqual(props["tags"], ("tags", "vrf-blue sn-1"))
        self.assertEqual(props["next-hop"], ("next_hop", "x"))
        self.assertNotIn("link", props)
        web = self.shapes["web"].find(f"{V}Section[@N='Hyperlink']/{V}Row")
        self.assertEqual(Package.cell(web, "Address"), "https://example.com/x")

    def test_html_label_to_text(self):
        self.assertEqual(vsdx._text("a<br>b&amp;c<div>d</div>​", True), "a\nb&c\nd")
        self.assertEqual(vsdx._text("​", True), "")


class IconTest(unittest.TestCase):
    def test_library_icons_become_pictures(self):
        lib = icon_library.load_icon_library(config.DEFAULT_ICON_LIBRARY_PATH)
        styles = {t: e.style for t, e in lib.items()}
        svg = styles["C9200-24P Front"]      # an SVG that only wraps a PNG
        png = styles["ASR1004 Front"]
        pkg = Package(mxfile(("p", "P", [box("s", 0, 0, 230, 42, "sw1", svg),
                                         box("r", 0, 100, 230, 42, "rtr1", png)])))
        self.assertEqual(pkg.counts["images"], 2)
        media = sorted(n for n in pkg.names if n.startswith("visio/media/"))
        self.assertEqual(media, ["visio/media/image1.png", "visio/media/image2.png"])
        for shape in pkg.shapes():
            self.assertEqual(shape.get("Type"), "Foreign")
            rel = shape.find(f"{V}ForeignData/{V}Rel")
            self.assertIsNotNone(rel)
        sw = pkg.shapes()[0]
        self.assertLessEqual(pkg.num(sw, "Width"), 230 / 96 + 1e-9)   # kept to its aspect
        self.assertLess(pkg.num(sw, "TxtPinY"), 0)                   # label below the picture

    def test_each_picture_is_stored_once(self):
        lib = icon_library.load_icon_library(config.DEFAULT_ICON_LIBRARY_PATH)
        icon = lib["C9200-24P Front"].style
        pkg = Package(mxfile(("p1", "P", [box(f"s{i}", 0, i * 60, 230, 42, f"sw{i}", icon)
                                          for i in range(3)]),
                             ("p2", "Q", [box("t", 0, 0, 230, 42, "t", icon)])))
        self.assertEqual(pkg.counts["images"], 1)
        self.assertEqual([n for n in pkg.names if n.startswith("visio/media/")],
                         ["visio/media/image1.png"])
        rels = pkg.xml("visio/pages/_rels/page1.xml.rels").findall(f"{PKG}Relationship")
        self.assertEqual(len(rels), 1)

    def test_other_images_become_plain_boxes(self):
        svg = base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="5" '
                               b'height="5"/></svg>').decode()
        pkg = Package(mxfile(("p", "P", [box("s", 0, 0, 96, 48, "x",
                                              f"shape=image;image=data:image/svg+xml,{svg};")])))
        (shape,) = pkg.shapes()
        self.assertEqual((shape.get("Type"), pkg.counts["images"]), ("Shape", 0))
        self.assertEqual(pkg.cell(shape, "LinePattern"), "1")


class MainTest(unittest.TestCase):
    """main.py writes topology.vsdx beside topology.drawio."""

    def run_main(self, tmp, *extra):
        template = Path(tmp) / "template.drawio"
        shutil.copy(support.FIXTURES / "template.drawio", template)
        argv = ["main.py", str(support.FACTS), tmp, f"--inventory={support.FIXTURES / 'inventory.yml'}",
                f"--template={template}", *extra]
        with mock.patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()) as out:
            try:
                main.main()
                code = 0
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_writes_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self.run_main(tmp)
            self.assertEqual(code, 0, out)
            self.assertIn("Wrote Visio copy", out)
            drawio = ET.parse(Path(tmp) / "topology.drawio").getroot()
            pkg = Package(drawio)
            with zipfile.ZipFile(Path(tmp) / "topology.vsdx") as z:
                self.assertEqual(len(ET.fromstring(z.read("visio/pages/page1.xml"))
                                     .findall(f"{V}Shapes/{V}Shape")), pkg.counts["shapes"])
            layers = [Package.cell(r, "Name") for r in pkg.xml("visio/pages/pages.xml").find(
                f"{V}Page").findall(f"{V}PageSheet/{V}Section[@N='Layer']/{V}Row")]
            self.assertIn("Background", layers)
            self.assertGreater(len(layers), 1)   # one per feature layer too

    def test_no_vsdx_removes_an_old_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "topology.vsdx").write_text("old")
            code, _ = self.run_main(tmp, "--no-vsdx")
            self.assertEqual(code, 0)
            self.assertFalse((Path(tmp) / "topology.vsdx").exists())
            self.assertTrue((Path(tmp) / "topology.drawio").exists())

    def test_visio_failure_keeps_drawio_and_exits_3(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(vsdx, "write", side_effect=ValueError("boom")):
            (Path(tmp) / "topology.vsdx").write_text("old")
            code, out = self.run_main(tmp)
            self.assertEqual(code, main.EXIT_VSDX_FAILED)
            self.assertIn("Visio output failed", out)
            self.assertFalse((Path(tmp) / "topology.vsdx").exists())
            self.assertTrue((Path(tmp) / "topology.drawio").exists())
            self.assertTrue((Path(tmp) / "template.drawio").exists())


@unittest.skipUnless(shutil.which("soffice"), "LibreOffice not installed")
class LibreOfficeTest(unittest.TestCase):
    def test_opens_every_page(self):
        lib = icon_library.load_icon_library(config.DEFAULT_ICON_LIBRARY_PATH)
        icon = next(iter(lib.values())).style
        root = mxfile(("p1", "One", [box("a", 0, 0, 96, 48, "A", icon)]),
                      ("p2", "Two", [box("b", 0, 0, 96, 48, "B")]))
        with tempfile.TemporaryDirectory() as tmp:
            vsdx.write(root, Path(tmp) / "t.vsdx")
            subprocess.run(["soffice", f"-env:UserInstallation=file://{tmp}/profile", "--headless",
                            "--convert-to", "pdf", "--outdir", tmp, str(Path(tmp) / "t.vsdx")],
                           capture_output=True, timeout=180)
            pdf = (Path(tmp) / "t.pdf").read_bytes()
        self.assertEqual(len(re.findall(rb"/Type\s*/Page\b", pdf)), 2)


if __name__ == "__main__":
    unittest.main()
