"""Node icons (scripts/icon_library.py): a folder of PNG files, and the
export of a draw.io shape library into one."""
import base64
import html
import io
import json
import shutil
import struct
import sys
import tempfile
import unittest
import zlib
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import support
import config
import diagram as dg
import icon_library
import main


def tiny_png(w=3, h=2):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def library_entry(title, mime, data):
    cell = (f'<mxGraphModel><root><mxCell id="2" style="shape=image;image=data:{mime},'
            f'{base64.b64encode(data).decode()};" vertex="1"/></root></mxGraphModel>')
    return {"xml": html.escape(cell), "w": 100, "h": 20, "title": title}


class FolderTest(unittest.TestCase):
    def test_loads_pngs_by_file_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sw a.png").write_bytes(tiny_png())
            (Path(tmp) / "notes.txt").write_text("x")
            (Path(tmp) / "broken.png").write_text("not a png")
            with redirect_stdout(io.StringIO()) as out:
                icons = icon_library.load(tmp)
            self.assertEqual(list(icons), ["sw a"])
            self.assertEqual((icons["sw a"].mime, icons["sw a"].label_below), ("image/png", True))
            self.assertEqual(icons["sw a"].png(), tiny_png())
            self.assertIn("broken.png", out.getvalue())

    def test_missing_folder(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(icon_library.load("/nonexistent/icons"), {})
        self.assertIn("no icon folder", out.getvalue())

    def test_find_with_or_without_extension(self):
        icons = {"C9200-24P Front": "pic"}
        self.assertEqual(icon_library.find(icons, "C9200-24P Front"), "pic")
        self.assertEqual(icon_library.find(icons, "C9200-24P Front.png"), "pic")
        self.assertIsNone(icon_library.find(icons, "C9300"))

    def test_project_icons_are_all_pngs(self):
        icons = icon_library.load(config.DEFAULT_ICON_DIR)
        self.assertIn("C9200-24P Front", icons)
        for name, icon in icons.items():
            self.assertIsNotNone(dg.png_size(icon.data), name)


class ExportTest(unittest.TestCase):
    def test_exports_pngs_and_pngs_wrapped_in_svg(self):
        png = tiny_png()
        svg = (b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
               b'<image width="3" height="2" xlink:href="data:image/png;base64,'
               + base64.b64encode(png) + b'"/></svg>')
        drawn = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="3" height="2"/></svg>'
        entries = [library_entry("Plain", "image/png", png),
                   library_entry("Wrapped/1", "image/svg+xml", svg),
                   library_entry("Vector", "image/svg+xml", drawn)]
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp) / "lib.xml"
            lib.write_text("<mxlibrary>" + json.dumps(entries) + "</mxlibrary>")
            with redirect_stdout(io.StringIO()) as out:
                written = icon_library.export(lib, Path(tmp) / "icons")
            self.assertEqual(written, ["Plain", "Wrapped-1"])
            self.assertEqual((Path(tmp) / "icons" / "Plain.png").read_bytes(), png)
            self.assertEqual((Path(tmp) / "icons" / "Wrapped-1.png").read_bytes(), png)
            self.assertIn("Vector", out.getvalue())


class RenderTest(unittest.TestCase):
    def test_inventory_icons_reach_both_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            inv = (support.FIXTURES / "inventory.yml").read_text()
            inv = inv.replace("hq: {}", 'hq:\n          icon: "C9200-24P Front.png"', 1)
            inv = inv.replace("core1: {}", 'core1:\n          icon: "No Such Icon"', 1)
            (tmp / "inventory.yml").write_text(inv)
            shutil.copy(support.FIXTURES / "template.vsdx", tmp / "template.vsdx")
            argv = ["main.py", str(support.FACTS), str(tmp), f"--inventory={tmp / 'inventory.yml'}",
                    f"--template={tmp / 'template.vsdx'}", f"--icons={config.DEFAULT_ICON_DIR}"]
            with mock.patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()) as out:
                main.main()
            self.assertIn("no icon 'No Such Icon'", out.getvalue())
            drawio_text = (tmp / "topology.drawio").read_text()
            self.assertEqual(drawio_text.count("shape=image;"), 1)
            from test_vsdx import Package
            pkg = Package(data=(tmp / "topology.vsdx").read_bytes())
            self.assertEqual([n for n in pkg.names if n.startswith("visio/media/")],
                             ["visio/media/image1.png"])


if __name__ == "__main__":
    unittest.main()
