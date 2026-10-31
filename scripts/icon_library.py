"""
Loads a draw.io custom shape library (the .xml file you get from
Extras > Edit Diagram on a library, or File > Open Library From >
Device... format: <mxlibrary>[{xml, w, h, aspect, title}, ...]</mxlibrary>)
and exposes each entry's picture by title, so renderer.py can show a
device's preferred icon instead of the plain rectangle (IconEntry.icon()
gives it as a diagram.Icon, for both the draw.io and the Visio writer).

Each library entry's "xml" field is an HTML-entity-encoded mxGraphModel
fragment. Simple entries are a single mxCell with
style="shape=image;...;image=data:image/svg+xml,<base64>;" - that style
string is exactly what we want to drop onto a node's cell.

Composite entries (e.g. rack-front photos imported from Visio) wrap that
image cell inside extra decorative cells and sometimes drop the
"shape=image" token entirely (a vsdx-import quirk - the style ends up as
bare "image;...;image=data:...;" instead of "shape=image;...;"). We find
the first descendant cell carrying an image=data: payload and normalize
the style so "shape=image" is always present. We do NOT attempt to
reconstruct any sibling decorative cells (outlines, bezels, etc.) - for
multi-cell composites you'll get just the photo, not the full illustration.
"""
import base64
import html
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import diagram


@dataclass
class IconEntry:
    title: str
    style: str    # ready-to-use mxCell style, e.g. "shape=image;...;image=data:image/svg+xml,...;"
    w: float
    h: float       # native size from the library - useful for aspect ratio, not authoritative sizing

    def icon(self) -> "diagram.Icon | None":
        """The entry's picture: the image= data URI's bytes and type, with
        the name below it when the library style says so."""
        m = re.search(r"image=data:(image/(?:png|svg\+xml)),([^;]+)", self.style)
        if not m:
            return None
        try:
            data = base64.b64decode(m.group(2))
        except ValueError:
            return None
        return diagram.Icon(data, m.group(1),
                            label_below="verticalLabelPosition=bottom" in self.style)


def _find_image_cell(xml_root):
    """Recursively find the first mxCell whose style carries an embedded
    image= payload, regardless of how deeply it's nested or wrapped in a
    UserObject."""
    for cell in xml_root.iter("mxCell"):
        if "image=data:" in cell.get("style", ""):
            return cell
    return None


def _normalize_style(style: str) -> str:
    """Ensure shape=image is present (some vsdx-imported entries have a
    bare 'image;' token instead), and drop imageAspect=0 so icons keep
    their native proportions and letterbox inside whatever fixed node box
    pathfinder gives them, rather than stretching to fill it."""
    if "shape=image" not in style:
        style = "shape=image;" + style
    style = re.sub(r"imageAspect=0;?", "", style)
    if not style.endswith(";"):
        style += ";"
    return style


def load_icon_library(path: str) -> dict[str, IconEntry]:
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    inner = re.sub(r"^\s*<mxlibrary>", "", raw)
    inner = re.sub(r"</mxlibrary>\s*$", "", inner.strip())
    entries = json.loads(inner)

    library: dict[str, IconEntry] = {}
    for entry in entries:
        title = entry.get("title", "<untitled>")
        try:
            xml_root = ET.fromstring(html.unescape(entry["xml"]))
        except ET.ParseError as e:
            print(f"icon_library: skipping '{title}' - malformed xml ({e})")
            continue

        cell = _find_image_cell(xml_root)
        if cell is None:
            print(f"icon_library: skipping '{title}' - no image= cell found")
            continue

        library[title] = IconEntry(
            title=title,
            style=_normalize_style(cell.get("style")),
            w=float(entry.get("w", 0)) or None,
            h=float(entry.get("h", 0)) or None,
        )

    return library


if __name__ == "__main__":
    import sys
    lib = load_icon_library(sys.argv[1] if len(sys.argv) > 1 else "../docs/Prod_Library.xml")
    print(f"\nLoaded {len(lib)} icon(s):")
    for title, entry in lib.items():
        print(f"  {title!r}: {entry.w}x{entry.h}, style[:80]={entry.style[:80]}...")
