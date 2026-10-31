"""The Visio writer (scripts/vsdx.py), from the format-independent diagram
(scripts/diagram.py): the package is a valid OPC zip whose parts and
relationships all resolve and that follows the structural rules of Visio's
schema; shapes land where the diagram puts them (inches, y flipped);
layers, tooltips, page links and data carry over; icons become pictures;
the writer doesn't depend on draw.io; and main.py writes each format on its
own. If LibreOffice is installed, it must open the file."""
import ast
import io
import math
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import support
import config
import diagram as dg
import drawio
import icon_library
import main
import vsdx

V = "{%s}" % vsdx.NS
PKG = "{%s}" % vsdx.PKG_REL_NS
CT = "{http://schemas.openxmlformats.org/package/2006/content-types}"


def page(pid="p1", name="Topology", items=(), layers=()):
    p = dg.Page(pid, name, layers=list(layers))
    for item in items:
        p.add(item)
    return p


def box(sid, x, y, w, h, text="", **kw):
    return dg.Shape(sid, x=x, y=y, w=w, h=h, text=text, **kw)


def icon(title):
    lib = icon_library.load_icon_library(config.DEFAULT_ICON_LIBRARY_PATH)
    return lib[title].icon()


class Package:
    """A written .vsdx, opened for checking."""

    def __init__(self, diagram=None, data=None):
        if data is None:
            buf = io.BytesIO()
            self.counts = vsdx.write(diagram, buf)
            data = buf.getvalue()
        self.zip = zipfile.ZipFile(io.BytesIO(data))
        self.names = set(self.zip.namelist())

    def xml(self, name):
        return ET.fromstring(self.zip.read(name))

    def shapes(self, n=1):
        return self.xml(f"visio/pages/page{n}.xml").findall(f"{V}Shapes/{V}Shape")

    def layer_rows(self, n=0):
        pg = self.xml("visio/pages/pages.xml").findall(f"{V}Page")[n]
        return pg.findall(f"{V}PageSheet/{V}Section[@N='Layer']/{V}Row")

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


# Visio's schema fixes the order of a sheet's children, and Visio refuses
# ("some parts are missing or invalid") a file that breaks it.
SHEET_ORDER = ["Cell", "Trigger", "Section", "Text", "Data1", "Data2", "Data3",
               "ForeignData", "Shapes"]


def schema_problems(pkg):
    """The structural rules of Visio's schema that this writer could break."""
    problems = []

    def ordered(el, order, where):
        tags = [child.tag.split("}")[1] for child in el]
        if any(t not in order for t in tags):
            problems.append(f"{where}: unexpected child in {tags}")
        elif [order.index(t) for t in tags] != sorted(order.index(t) for t in tags):
            problems.append(f"{where}: children out of order: {' '.join(tags)}")

    for name in sorted(pkg.names):
        if not name.endswith(".xml") or not name.startswith("visio/"):
            continue
        root = pkg.xml(name)
        for tag in ("Shape", "StyleSheet", "PageSheet"):
            for sheet in root.iter(V + tag):
                ordered(sheet, SHEET_ORDER, f"{name} {tag} {sheet.get('ID', '')}")
        for section in root.iter(V + "Section"):
            ordered(section, ["Cell", "Row"], f"{name} Section {section.get('N')}")
        for cell in root.iter(V + "Cell"):
            if cell.get("N") is None or cell.get("V") is None:
                problems.append(f"{name}: Cell without N or V")
        ids = [s.get("ID") for s in root.iter(V + "Shape")]
        if len(ids) != len(set(ids)):
            problems.append(f"{name}: duplicate shape IDs")
        folder, base = name.rsplit("/", 1)
        rels_name = f"{folder}/_rels/{base}.rels"
        rids = ({r.get("Id") for r in pkg.xml(rels_name).findall(f"{PKG}Relationship")}
                if rels_name in pkg.names else set())
        for rel in root.iter(V + "Rel"):
            rid = rel.get("{%s}id" % vsdx.R_NS)
            if rid not in rids:
                problems.append(f"{name}: Rel {rid} has no relationship")
    for part in ("visio/document.xml", "visio/pages/pages.xml", "visio/windows.xml",
                 "docProps/core.xml", "docProps/app.xml"):
        if part not in pkg.names:
            problems.append(f"missing {part}")
    return problems


def every_kind():
    """One page with every kind of item the writers handle, and a second."""
    lay = dg.Layer("layer-x", "X", locked=True)
    items = [
        box("a", 0, 0, 230, 42, "sw1", icon=icon("C9200-24P Front")),
        box("b", 300, 200, 96, 48, "B", style={"rounded": True}),
        box("t", 0, 300, 96, 48, "tip", layer="layer-x", tooltip="hover", link="page:p2",
            data={"k": "v"}, tags=("sn-1",)),
        box("e", 200, 0, 14, 14, style={"kind": "ellipse", "fill": "#FF0000"}),
        dg.Line("l", source="a", target="b", waypoints=[(200, 100)], tooltip="a link",
                link="page:p2#highlight=sn-1", style={"curved": True, "dashed": True}),
        dg.LineLabel("lbl", line="l", at=-1, text="Gi1",
                     style={"kind": "text", "rotation": -90, "label_bg": "#00CC00"}),
    ]
    return dg.Diagram([page("p1", "One", items, [lay]),
                       page("p2", "Two", [box("z", 0, 0, 96, 48, "Z")])])


class PackageTest(unittest.TestCase):
    def setUp(self):
        self.pkg = Package(dg.Diagram([page("p1", "Topology", [box("a", 0, 0, 96, 48, "A")]),
                                       page("p2", "Routes · A", [box("b", 0, 0, 96, 48, "B")])]))

    def test_every_part_is_xml_and_every_relationship_resolves(self):
        z = self.pkg
        for name in z.names:
            if name.endswith((".xml", ".rels")):
                z.xml(name)   # parses
        for rels in [n for n in z.names if n.endswith(".rels")]:
            base = rels.replace("_rels/", "").rsplit(".rels", 1)[0]
            folder = base.rsplit("/", 1)[0] + "/" if "/" in base else ""
            for rel in z.xml(rels).findall(f"{PKG}Relationship"):
                path = Path(folder + rel.get("Target")).as_posix()
                while "/../" in path:
                    path = re.sub(r"[^/]+/\.\./", "", path, count=1)
                self.assertIn(path, z.names, f"{rels} -> {rel.get('Target')}")

    def test_content_types_cover_each_part(self):
        types = self.pkg.xml("[Content_Types].xml")
        overrides = {o.get("PartName") for o in types.findall(f"{CT}Override")}
        self.assertTrue({"/visio/document.xml", "/visio/pages/pages.xml", "/visio/windows.xml",
                         "/visio/pages/page1.xml", "/visio/pages/page2.xml"} <= overrides)
        for part in overrides:
            self.assertIn(part.lstrip("/"), self.pkg.names)

    def test_pages_keep_their_names_and_order(self):
        pages = self.pkg.xml("visio/pages/pages.xml").findall(f"{V}Page")
        self.assertEqual([p.get("Name") for p in pages], ["Topology", "Routes · A"])
        self.assertEqual(self.pkg.counts, {"pages": 2, "shapes": 2, "images": 0})

    def test_duplicate_page_names_are_made_unique(self):
        pkg = Package(dg.Diagram([page("p1", "Page"), page("p2", "Page")]))
        names = [p.get("Name") for p in pkg.xml("visio/pages/pages.xml").findall(f"{V}Page")]
        self.assertEqual(names, ["Page", "Page (2)"])

    def test_follows_the_schema(self):
        self.assertEqual(schema_problems(Package(every_kind())), [])

    def test_does_not_depend_on_drawio(self):
        tree = ast.parse((support.SCRIPTS / "vsdx.py").read_text())
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertFalse({"drawio", "drawio_position_loader"} & imported)
        with mock.patch.dict(sys.modules, {"drawio": None}):
            Package(every_kind())   # works with draw.io's writer unavailable


class GeometryTest(unittest.TestCase):
    def test_inches_and_y_flipped(self):
        # two boxes 2 in apart; the page is their bounds plus a 0.5 in margin
        pkg = Package(dg.Diagram([page(items=[box("a", 100, 100, 96, 48, "top"),
                                              box("b", 100, 292, 192, 96, "low")])]))
        a, b = pkg.shapes()
        sheet = pkg.xml("visio/pages/pages.xml").find(f"{V}Page/{V}PageSheet")
        self.assertAlmostEqual(pkg.num(sheet, "PageWidth"), 2 + 1)
        self.assertAlmostEqual(pkg.num(sheet, "PageHeight"), (388 - 100) / 96 + 1)
        self.assertAlmostEqual(pkg.num(a, "Width"), 1)
        self.assertAlmostEqual(pkg.num(a, "PinX"), 0.5 + 0.5)
        self.assertGreater(pkg.num(a, "PinY"), pkg.num(b, "PinY"))   # y points up
        self.assertAlmostEqual(pkg.num(b, "PinY"), 0.5 + 0.5)

    def test_rotation_ellipse_and_rounding(self):
        pkg = Package(dg.Diagram([page(items=[
            box("r", 0, 0, 40, 20, "Gi1", style={"kind": "text", "rotation": -90,
                                                 "label_bg": "#00CC00", "label_border": "#000000"}),
            box("e", 100, 0, 14, 14, style={"kind": "ellipse", "fill": "#ff0000"}),
            box("p", 200, 0, 100, 40, "pill", style={"rounded": True, "arc": 50})])]))
        rot, dot, pill = pkg.shapes()
        self.assertAlmostEqual(pkg.num(rot, "Angle"), math.pi / 2, places=5)
        self.assertEqual(pkg.cell(rot, "FillForegnd"), "#00CC00")
        self.assertEqual(pkg.cell(rot, "LinePattern"), "1")
        self.assertEqual(pkg.cell(dot, "FillForegnd"), "#FF0000")
        self.assertIsNotNone(dot.find(f"{V}Section[@N='Geometry']/{V}Row[@T='Ellipse']"))
        arcs = pill.findall(f"{V}Section[@N='Geometry']/{V}Row[@T='ArcTo']")
        self.assertEqual(len(arcs), 4)   # corner radius = half the height: a pill

    def test_line_follows_routed_points_and_label_sits_on_it(self):
        pkg = Package(dg.Diagram([page(items=[
            box("a", 0, 0, 96, 96, "A"), box("b", 288, 192, 96, 96, "B"),
            dg.Line("e", source="a", target="b", waypoints=[(336, 48)], exit=(1, 0.5),
                    entry=(0.5, 0), style={"end_arrow": "none", "dashed": True}),
            dg.LineLabel("l", line="e", at=0, text="mid", style={"kind": "text"})])]))
        _, _, line, label = pkg.shapes()
        rows = line.findall(f"{V}Section[@N='Geometry']/{V}Row")
        left = pkg.num(line, "PinX") - pkg.num(line, "Width") / 2
        bottom = pkg.num(line, "PinY") - pkg.num(line, "Height") / 2
        pts = [(round(left + float(Package.cell(r, "X")), 4),
                round(bottom + float(Package.cell(r, "Y")), 4)) for r in rows]
        page_h = 288 / 96 + 1
        want = [(96, 48), (336, 48), (336, 192)]   # exit, waypoint, entry
        self.assertEqual(pts, [(round(x / 96 + 0.5, 4), round(page_h - (y / 96 + 0.5), 4))
                               for x, y in want])
        self.assertEqual((pkg.cell(line, "EndArrow"), pkg.cell(line, "LinePattern")), ("0", "2"))
        # half way along the 240 + 144 pixel line is 192 in: (288, 48)
        self.assertAlmostEqual(pkg.num(label, "PinX"), 288 / 96 + 0.5)
        self.assertAlmostEqual(pkg.num(label, "PinY"), page_h - (48 / 96 + 0.5))

    def test_default_arrow_and_curves(self):
        pkg = Package(dg.Diagram([page(items=[
            box("a", 0, 0, 96, 96), box("b", 400, 400, 96, 96),
            dg.Line("e1", source="a", target="b"),
            dg.Line("e2", source="a", target="b", waypoints=[(300, 40)],
                    style={"curved": True, "end_arrow": "none"})])]))
        straight, curved = pkg.shapes()[2:]
        self.assertEqual(pkg.cell(straight, "EndArrow"), str(vsdx.ARROWS["classic"]))
        self.assertGreater(len(curved.findall(f"{V}Section[@N='Geometry']/{V}Row")), 8)


class DataTest(unittest.TestCase):
    def setUp(self):
        lay = dg.Layer("layer-vrf", "VRF · blue", visible=False, locked=True)
        items = [box("a", 0, 0, 96, 48, "A"),
                 box("dot", 0, 100, 14, 14, "1", layer="layer-vrf",
                     style={"kind": "ellipse"}, tooltip="10.0.0.0/24\nvia ospf",
                     link="page:p2#highlight=sn-1", tags=("vrf-blue", "sn-1"),
                     data={"vrf": "blue", "next-hop": "x"}),
                 box("w", 200, 0, 96, 48, "web", link="https://example.com/x")]
        self.pkg = Package(dg.Diagram([page("p1", "Topology", items, [lay]),
                                       page("p2", "Subnets")]))
        self.shapes = {Package.text(s): s for s in self.pkg.shapes()}

    def test_layers(self):
        got = [(Package.cell(r, "Name"), Package.cell(r, "Visible"), Package.cell(r, "Lock"))
               for r in self.pkg.layer_rows()]
        self.assertEqual(got, [("Background", "1", "0"), ("VRF · blue", "0", "1")])
        self.assertIsNone(Package.cell(self.shapes["A"], "LayerMember"))
        self.assertEqual(Package.cell(self.shapes["1"], "LayerMember"), "1")

    def test_tooltip_link_and_data(self):
        dot = self.shapes["1"]
        self.assertEqual(Package.cell(dot, "Comment"), "10.0.0.0/24\nvia ospf")
        link = dot.find(f"{V}Section[@N='Hyperlink']/{V}Row")
        self.assertEqual(Package.cell(link, "SubAddress"), "Subnets")   # the page it opens
        props = {Package.cell(r, "Label"): (r.get("N"), Package.cell(r, "Value"))
                 for r in dot.findall(f"{V}Section[@N='Property']/{V}Row")}
        self.assertEqual(props["tags"], ("tags", "vrf-blue sn-1"))
        self.assertEqual(props["next-hop"], ("next_hop", "x"))
        web = self.shapes["web"].find(f"{V}Section[@N='Hyperlink']/{V}Row")
        self.assertEqual(Package.cell(web, "Address"), "https://example.com/x")


class IconTest(unittest.TestCase):
    def test_library_icons_become_pictures(self):
        pkg = Package(dg.Diagram([page(items=[
            box("s", 0, 0, 230, 42, "sw1", icon=icon("C9200-24P Front")),     # SVG wrapping a PNG
            box("r", 0, 100, 230, 42, "rtr1", icon=icon("ASR1004 Front"))])]))  # a PNG
        self.assertEqual(pkg.counts["images"], 2)
        media = sorted(n for n in pkg.names if n.startswith("visio/media/"))
        self.assertEqual(media, ["visio/media/image1.png", "visio/media/image2.png"])
        for shape in pkg.shapes():
            self.assertEqual(shape.get("Type"), "Foreign")
            self.assertIsNotNone(shape.find(f"{V}ForeignData/{V}Rel"))
        sw = pkg.shapes()[0]
        self.assertLessEqual(pkg.num(sw, "Width"), 230 / 96 + 1e-9)   # kept to its aspect
        self.assertLess(pkg.num(sw, "TxtPinY"), 0)                   # name below the picture

    def test_each_picture_is_stored_once(self):
        pic = icon("C9200-24P Front")
        pkg = Package(dg.Diagram([
            page("p1", "P", [box(f"s{i}", 0, i * 60, 230, 42, f"sw{i}", icon=pic)
                             for i in range(3)]),
            page("p2", "Q", [box("t", 0, 0, 230, 42, "t", icon=pic)])]))
        self.assertEqual(pkg.counts["images"], 1)
        rels = pkg.xml("visio/pages/_rels/page1.xml.rels").findall(f"{PKG}Relationship")
        self.assertEqual(len(rels), 1)

    def test_other_images_become_plain_boxes(self):
        svg = dg.Icon(b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="5" height="5"/></svg>',
                      "image/svg+xml")
        pkg = Package(dg.Diagram([page(items=[box("s", 0, 0, 96, 48, "x", icon=svg)])]))
        (shape,) = pkg.shapes()
        self.assertEqual((shape.get("Type"), pkg.counts["images"]), ("Shape", 0))
        self.assertEqual(pkg.cell(shape, "LinePattern"), "1")


class OutputsTest(unittest.TestCase):
    """main.py writes topology.vsdx and topology.drawio, each on its own."""

    def run_main(self, tmp, *extra):
        template = Path(tmp) / "template.vsdx"
        shutil.copy(support.FIXTURES / "template.vsdx", template)
        argv = ["main.py", str(support.FACTS), tmp, f"--inventory={support.FIXTURES / 'inventory.yml'}",
                f"--template={template}", *extra]
        with mock.patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()) as out:
            try:
                main.main()
                code = 0
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_writes_both_with_the_same_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self.run_main(tmp)
            self.assertEqual(code, 0, out)
            pkg = Package(data=(Path(tmp) / "topology.vsdx").read_bytes())
            self.assertEqual(schema_problems(pkg), [])
            root = ET.parse(Path(tmp) / "topology.drawio").getroot().find(".//root")
            drawn = [c for c in root.iter("mxCell") if c.get("vertex") == "1" or c.get("edge") == "1"]
            self.assertEqual(len(pkg.shapes()), len(drawn))
            vsdx_layers = [Package.cell(r, "Name") for r in pkg.layer_rows()][1:]
            drawio_layers = [c.get("value") for c in root.findall("mxCell")
                             if c.get("parent") == "0" and c.get("id") != "1"]
            self.assertEqual(vsdx_layers, drawio_layers)
            self.assertIn("IPsec tunnels", vsdx_layers)

    def test_one_format_only_removes_the_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "topology.drawio").write_text("old")
            code, _ = self.run_main(tmp, "--formats=vsdx")
            self.assertEqual(code, 0)
            self.assertTrue((Path(tmp) / "topology.vsdx").exists())
            self.assertFalse((Path(tmp) / "topology.drawio").exists())
            code, _ = self.run_main(tmp, "--formats=drawio")
            self.assertFalse((Path(tmp) / "topology.vsdx").exists())
            self.assertTrue((Path(tmp) / "topology.drawio").exists())

    def test_unknown_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = self.run_main(tmp, "--formats=pdf")
            self.assertIn("pdf", str(code))

    def test_a_failing_writer_leaves_the_others(self):
        for broken, kept in (("vsdx", "topology.drawio"), ("drawio", "topology.vsdx")):
            name = main.WRITERS[broken][0]
            writers = dict(main.WRITERS)
            writers[broken] = (name, mock.Mock(side_effect=ValueError("boom")))
            with self.subTest(broken=broken), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(main, "WRITERS", writers):
                (Path(tmp) / name).write_text("old")
                code, out = self.run_main(tmp)
                self.assertEqual(code, main.EXIT_OUTPUT_FAILED)
                self.assertIn(f"{name} couldn't be written", out)
                self.assertFalse((Path(tmp) / name).exists())
                self.assertTrue((Path(tmp) / kept).exists())
                self.assertTrue((Path(tmp) / "template.vsdx").exists())


@unittest.skipUnless(shutil.which("soffice"), "LibreOffice not installed")
class LibreOfficeTest(unittest.TestCase):
    def test_opens_every_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            vsdx.write(every_kind(), Path(tmp) / "t.vsdx")
            subprocess.run(["soffice", f"-env:UserInstallation=file://{tmp}/profile", "--headless",
                            "--convert-to", "pdf", "--outdir", tmp, str(Path(tmp) / "t.vsdx")],
                           capture_output=True, timeout=180)
            pdf = (Path(tmp) / "t.pdf").read_bytes()
        self.assertEqual(len(re.findall(rb"/Type\s*/Page\b", pdf)), 2)


if __name__ == "__main__":
    unittest.main()
