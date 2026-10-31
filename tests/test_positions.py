"""The position template (scripts/positions.py): a Visio file holding one
shape per node, read at the start of a run and rewritten at the end."""
import io
import shutil
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import support
import diagram as dg
import main
import positions
from test_vsdx import Package, icon, schema_problems

INVENTORY = support.FIXTURES / "inventory.yml"
NS = "http://schemas.microsoft.com/office/visio/2012/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def nodes():
    return [dg.Shape("hq", style={"wrap": True}, x=285, y=0, w=230, h=42),
            dg.Shape("core1", style={"wrap": True}, x=285.5, y=200.25, w=230, h=42,
                     icon=icon("C9200-24P Front")),
            dg.Shape("segment 10.0.0.0/24", style={"rounded": True}, x=-40, y=500, w=160, h=40)]


def visio_saved(page_xml, sheet="", target="page1.xml"):
    """A minimal .vsdx shaped like one Visio saved: just the parts the
    reader needs, with the page body given."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("visio/pages/pages.xml",
                   f'<Pages xmlns="{NS}" xmlns:r="{R}"><Page ID="0" Name="Positions">'
                   f'<PageSheet><Cell N="PageWidth" V="20"/><Cell N="PageHeight" V="10" U="IN"/>'
                   f'{sheet}</PageSheet><Rel r:id="rId1"/></Page></Pages>')
        z.writestr("visio/pages/_rels/pages.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.microsoft.com/visio/2010/relationships/page" '
                   f'Target="{target}"/></Relationships>')
        part = target.lstrip("/") if target.startswith("/") else "visio/pages/" + target
        z.writestr(part, f'<PageContents xmlns="{NS}"><Shapes>{page_xml}</Shapes></PageContents>')
    return buf.getvalue()


def shape(text, pin, size=(1, 0.5), loc=None, extra="", inner=""):
    loc = loc or (size[0] / 2, size[1] / 2)
    return (f'<Shape ID="1" Type="{"Group" if inner else "Shape"}">'
            f'<Cell N="PinX" V="{pin[0]}" F="GUARD(1)"/><Cell N="PinY" V="{pin[1]}"/>'
            f'<Cell N="Width" V="{size[0]}"/><Cell N="Height" V="{size[1]}"/>'
            f'<Cell N="LocPinX" V="{loc[0]}" F="Width*0.5"/><Cell N="LocPinY" V="{loc[1]}"/>'
            f'{extra}<Text><cp IX="0"/>{text}\n</Text>'
            + (f"<Shapes>{inner}</Shapes>" if inner else "") + "</Shape>")


class RoundTripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "template.vsdx"
        positions.write_template(self.path, nodes(), {"core1": "left"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_positions_and_sides_come_back_exactly(self):
        found = {name: (x, y, data) for name, x, y, data in positions.read_shapes(self.path)}
        self.assertEqual(found["hq"], (400.0, 21.0, {"side": ""}))
        self.assertEqual(found["core1"], (400.5, 221.25, {"side": "left"}))
        self.assertEqual(found["segment 10.0.0.0/24"][:2], (40.0, 520.0))

    def test_load_checks_names_against_the_inventory(self):
        pinned = positions.load(self.path, INVENTORY)
        self.assertEqual(pinned["core1"], {"x": 400.5, "y": 221.25, "side": "left"})
        self.assertNotIn("side", pinned["hq"])
        self.assertIn("segment 10.0.0.0/24", pinned)

    def test_moving_and_setting_side_as_in_visio(self):
        support.edit_template_shape(self.path, "hq", x=1000, y=300, side="Bottom")
        pinned = positions.load(self.path, INVENTORY)
        self.assertEqual(pinned["hq"], {"x": 1115.0, "y": 321.0, "side": "bottom"})

    def test_is_a_valid_visio_file_with_icons(self):
        pkg = Package(data=self.path.read_bytes())
        self.assertEqual(schema_problems(pkg), [])
        self.assertEqual(len(pkg.shapes()), 3)
        self.assertTrue(any(n.startswith("visio/media/") for n in pkg.names))

    def test_unknown_names_are_refused(self):
        positions.write_template(self.path, [dg.Shape("hq-typo", w=10, h=10)], {})
        with self.assertRaises(ValueError) as e:
            positions.load(self.path, INVENTORY)
        self.assertIn("hq-typo", str(e.exception))

    def test_a_bad_side_is_ignored(self):
        support.edit_template_shape(self.path, "hq", side="diagonal")
        with redirect_stdout(io.StringIO()) as out:
            pinned = positions.load(self.path, INVENTORY)
        self.assertNotIn("side", pinned["hq"])
        self.assertIn("diagonal", out.getvalue())


class VisioSavedFileTest(unittest.TestCase):
    """Things Visio does to a file when it's saved there."""

    def read(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.vsdx"
            p.write_bytes(data)
            return {n: (x, y, d) for n, x, y, d in positions.read_shapes(p)}

    def test_no_origin_cells_means_page_corner_at_zero(self):
        got = self.read(visio_saved(shape("hq", (2, 9))))
        self.assertEqual(got["hq"][:2], (192.0, 96.0))

    def test_origin_cells_are_added(self):
        sheet = ('<Section N="User"><Row N="OriginX"><Cell N="Value" V="-48"/></Row>'
                 '<Row N="OriginY"><Cell N="Value" V="100"/></Row></Section>')
        got = self.read(visio_saved(shape("hq", (2, 9)), sheet))
        self.assertEqual(got["hq"][:2], (144.0, 196.0))

    def test_pin_off_centre(self):
        got = self.read(visio_saved(shape("hq", (2, 9), size=(2, 1), loc=(0, 0))))
        self.assertEqual(got["hq"][:2], (288.0, 48.0))

    def test_shapes_inside_a_group(self):
        group = shape("", (5, 5), size=(4, 2), inner=shape("core1", (1, 1)))
        got = self.read(visio_saved(group))
        # group's left/bottom is at 3, 4 in; the child's centre 1, 1 inside it
        self.assertEqual(got["core1"][:2], (384.0, 480.0))

    def test_side_in_a_renamed_row(self):
        prop = ('<Section N="Property"><Row N="Row_1"><Cell N="Value" V="top" U="STR"/>'
                '<Cell N="Label" V="Side"/></Row></Section>')
        got = self.read(visio_saved(shape("hq", (2, 9), extra=prop)))
        self.assertEqual(got["hq"][2], {"side": "top"})

    def test_absolute_part_target(self):
        got = self.read(visio_saved(shape("hq", (2, 9)), target="/visio/pages/page7.xml"))
        self.assertIn("hq", got)

    def test_not_a_visio_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.vsdx"
            p.write_text("not a zip")
            with self.assertRaises(ValueError):
                positions.read_shapes(p)


class MainTest(unittest.TestCase):
    def run_main(self, tmp, template):
        argv = ["main.py", str(support.FACTS), str(tmp), f"--inventory={INVENTORY}",
                f"--template={template}", "--formats=vsdx"]
        with mock.patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()) as out:
            try:
                main.main()
                return 0, out.getvalue()
            except SystemExit as e:
                return e.code, out.getvalue()

    def centres(self, path):
        return {n: (x, y) for n, x, y, _ in positions.read_shapes(path)}

    def test_upgrades_from_template_drawio(self):
        """No template.vsdx yet, but a template.drawio next to it: the old
        positions are read once and template.vsdx carries them on."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            shutil.copy(support.FIXTURES / "template.drawio", tmp / "template.drawio")
            code, out = self.run_main(tmp, tmp / "template.vsdx")
            self.assertEqual(code, 0, out)
            self.assertIn("this once", out)
            self.assertEqual(self.centres(tmp / "template.vsdx"),
                             self.centres(support.FIXTURES / "template.vsdx"))

    def test_a_drawio_template_path_writes_vsdx_next_to_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            shutil.copy(support.FIXTURES / "template.drawio", tmp / "layout.drawio")
            code, out = self.run_main(tmp, tmp / "layout.drawio")
            self.assertEqual(code, 0, out)
            self.assertTrue((tmp / "layout.vsdx").exists())

    def test_positions_survive_a_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            shutil.copy(support.FIXTURES / "template.vsdx", tmp / "template.vsdx")
            code, out = self.run_main(tmp, tmp / "template.vsdx")
            self.assertEqual(code, 0, out)
            self.assertEqual(self.centres(tmp / "template.vsdx"),
                             self.centres(support.FIXTURES / "template.vsdx"))

    def test_a_failed_template_write_keeps_the_old_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            shutil.copy(support.FIXTURES / "template.vsdx", tmp / "template.vsdx")
            before = (tmp / "template.vsdx").read_bytes()
            with mock.patch.object(positions, "template_diagram", side_effect=ValueError("boom")):
                code, out = self.run_main(tmp, tmp / "template.vsdx")
            self.assertEqual(code, main.EXIT_OUTPUT_FAILED)
            self.assertIn("position template couldn't be written", out)
            self.assertEqual((tmp / "template.vsdx").read_bytes(), before)
            self.assertTrue((tmp / "topology.vsdx").exists())


if __name__ == "__main__":
    unittest.main()
