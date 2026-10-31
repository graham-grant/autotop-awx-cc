"""
Node icons: a folder of PNG files (icons/ at the project root by default).

A host's `icon` in inventory.yml names a file in the folder, without or
with its .png extension: icon: "C9200-24P Front" shows
icons/C9200-24P Front.png. load(folder) returns {name: diagram.Icon}, which
renderer.py puts on the node for both the Visio and the draw.io writer. The
picture is letterboxed into the node's box (its size comes from the
inventory), with the device name below it.

To add an icon, drop a PNG into the folder. A transparent background and a
few hundred pixels across is plenty; every node that uses the icon in the
draw.io file carries its own copy, so smaller files keep that file small.

Exporting from a draw.io library: icons used to come from a draw.io shape
library (docs/Prod_Library.xml, <mxlibrary>[{xml, w, h, title}, ...]
</mxlibrary>). `python3 scripts/icon_library.py export LIBRARY.xml FOLDER`
writes each entry's picture as FOLDER/<title>.png: an entry's image is
either a PNG or an SVG that only wraps a PNG, and the PNG inside is written
as it is. An entry with any other picture is reported and skipped.
"""
from __future__ import annotations

import base64
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import diagram


def load(folder) -> dict:
    """{name: diagram.Icon} for every PNG in `folder` (the file name
    without .png). A missing folder gives {}, with a note in the log."""
    folder = Path(folder)
    if not folder.is_dir():
        print(f"icons: no icon folder at {folder}; nodes are drawn as boxes")
        return {}
    icons = {}
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() != ".png" or not path.is_file():
            continue
        data = path.read_bytes()
        if not diagram.png_size(data):
            print(f"icons: skipping {path.name} - not a PNG file")
            continue
        icons[path.stem] = diagram.Icon(data, "image/png", label_below=True)
    return icons


def find(icons: dict, name: str):
    """The icon an inventory `icon` value names (with or without .png)."""
    if name in icons:
        return icons[name]
    if name.lower().endswith(".png"):
        return icons.get(name[:-4])
    return None


# ---------------------------------------------------------------------------
# Export from a draw.io shape library
# ---------------------------------------------------------------------------

def read_drawio_library(path) -> dict:
    """{title: PNG bytes or None} for each entry of a draw.io library: the
    entry's picture as a PNG, or None if it isn't a PNG or an SVG wrapping
    one."""
    raw = Path(path).read_text(encoding="utf-8").strip()
    raw = re.sub(r"^<mxlibrary>|</mxlibrary>$", "", raw)
    out = {}
    for entry in json.loads(raw):
        title = entry.get("title") or "untitled"
        png = None
        try:
            root = ET.fromstring(html.unescape(entry["xml"]))
        except (KeyError, ET.ParseError):
            root = None
        for cell in root.iter("mxCell") if root is not None else ():
            m = re.search(r"image=data:(image/(?:png|svg\+xml)),([^;]+)", cell.get("style", ""))
            if m:
                try:
                    png = diagram.Icon(base64.b64decode(m.group(2)), m.group(1)).png()
                except ValueError:
                    png = None
                break
        out[title] = png
    return out


def export(library, folder) -> list:
    """Write each library entry's picture to folder/<title>.png. Returns
    the titles written."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for title, png in read_drawio_library(library).items():
        name = re.sub(r'[\\/:*?"<>|]', "-", title).strip()
        if png is None:
            print(f"icons: skipping '{title}' - its picture isn't a PNG or an SVG wrapping one")
            continue
        (folder / f"{name}.png").write_bytes(png)
        written.append(name)
    return written


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "export":
        names = export(sys.argv[2], sys.argv[3])
        print(f"Wrote {len(names)} icon(s) to {sys.argv[3]}: {', '.join(names)}")
    else:
        sys.exit("usage: python3 scripts/icon_library.py export LIBRARY.xml FOLDER")
