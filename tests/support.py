"""Shared test setup: puts scripts/ on sys.path and points at fixtures."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FACTS = FIXTURES / "facts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def edit_template_shape(path, name, x=None, y=None, side=None):
    """Do what a person does in Visio to the position template: move the
    shape whose text is `name` so its top-left corner is at x, y (diagram
    pixels), and/or set its Side Shape Data field. Rewrites the file."""
    import io
    import xml.etree.ElementTree as ET
    import zipfile
    import positions

    v = "{http://schemas.microsoft.com/office/visio/2012/main}"
    ET.register_namespace("", v[1:-1])
    ET.register_namespace("r", "http://schemas.openxmlformats.org/officeDocument/2006/relationships")
    path = Path(path)
    with zipfile.ZipFile(path) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    sheet = ET.fromstring(parts["visio/pages/pages.xml"]).find(f"{v}Page/{v}PageSheet")
    page_h = float(sheet.find(f"{v}Cell[@N='PageHeight']").get("V"))
    origin = {r.get("N"): float(r.find(f"{v}Cell[@N='Value']").get("V"))
              for r in sheet.findall(f"{v}Section[@N='User']/{v}Row")}
    page = ET.fromstring(parts["visio/pages/page1.xml"])
    for sh in page.iter(f"{v}Shape"):
        text = sh.find(f"{v}Text")
        if text is None or "".join(text.itertext()).strip() != name:
            continue
        cell = {c.get("N"): c for c in sh.findall(f"{v}Cell")}
        w, h = float(cell["Width"].get("V")), float(cell["Height"].get("V"))
        px = positions.vsdx.PX_PER_INCH
        if x is not None:
            cell["PinX"].set("V", str((x - origin["OriginX"]) / px + w / 2))
        if y is not None:
            cell["PinY"].set("V", str(page_h - (y - origin["OriginY"]) / px - h / 2))
        if side is not None:
            for row in sh.findall(f"{v}Section[@N='Property']/{v}Row"):
                if row.get("N") == "Side":
                    row.find(f"{v}Cell[@N='Value']").set("V", side)
        break
    else:
        raise KeyError(name)
    parts["visio/pages/page1.xml"] = ET.tostring(page, xml_declaration=True, encoding="utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, data in parts.items():
            z.writestr(n, data)
    path.write_bytes(buf.getvalue())
