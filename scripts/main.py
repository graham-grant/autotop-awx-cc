#!/usr/bin/env python3
"""
Build the topology diagram, as a Microsoft Visio file and a draw.io file,
from the per-host JSON files written by playbooks/gather_topology.yml.

This is the coordinator: load data (parser.py), decide node positions and
route every link through the Hanan-grid A* engine (pathfinder.py), draw
the nodes, links and interface labels into a format-independent diagram
(renderer.py, diagram.py), let each enabled protocol feature (features/ -
e.g. IPsec tunnels) draw itself onto its own layer, then write the diagram
with each output format's own writer: vsdx.py (topology.vsdx) and
drawio.py (topology.drawio). Neither writer reads the other's file.
Tunable settings live in config.py.

Runs on a base Python 3 install: no third-party packages. Host JSON is a
schema 2 record per device (drivers/; older files are upgraded on load,
and --reparse re-parses each file's raw output), inventory.yml is read by
miniyaml.py, and the A* router's search runs in a C extension that ships
prebuilt (_hanan.abi3.so, CPython 3.9+ stable ABI) - no compiler needed.

Links are derived from directly-connected subnets: every interface IP
the device reports is grouped by network, and any subnet shared by exactly two
inventory hosts becomes a link between them. A subnet shared by three or
more hosts becomes a small segment node linked to each of them.

Node positions are read from the position template, template.vsdx
(positions.py): a Visio file with one shape per node and no links or
labels. Reposition nodes by opening it in Visio, dragging them around, and
saving - the next run picks the new positions straight up. A node can also
get a preferred attachment side (top/bottom/left/right), overriding the
automatic per-link choice, in its "Side" Shape Data field - this
round-trips the same way positions do. An older template.drawio next to it
is read once, if template.vsdx doesn't exist yet.

The template itself is regenerated every run to match whatever's
currently in inventory.yml: existing nodes keep whatever position you last
left them at, new nodes appear in a simple staging grid below the rest,
ready to be dragged into place. There's no positioning library involved
(N2G has been removed) - an unplaced node just needs to be visible
somewhere reasonable, not laid out well, since you're expected to
reposition it by hand once.

Usage:
    python3 main.py [facts_dir] [output_dir] [--inventory=PATH] [--template=PATH]
                    [--icon-library=PATH] [--reparse] [--formats=vsdx,drawio]

Defaults: facts_dir=output/facts, output_dir=output,
inventory=<project root>/inventory.yml, template=<project root>/template.vsdx,
icon-library=<project root>/docs/Prod_Library.xml

Each host can get a preferred icon by adding an `icon:` key to its entry
in inventory.yml (or a `vars: {icon: ...}` block on a group, inherited by
every host under it) - the value must match an entry's title in the icon
library exactly, e.g. icon: "C9200-24P Front". A host with no icon set
falls back to the plain rectangle.

--formats (default config.OUTPUT_FORMATS) picks the files written:
"vsdx" for topology.vsdx, "drawio" for topology.drawio. An output file of
a format that isn't picked is deleted, so an old one is never mistaken for
this run's. If a writer fails, its file is deleted, the other formats and
the template are still written, and the exit status is 3. If the template
can't be written, the old one is left as it was and the exit status is 3.
"""
import sys
from pathlib import Path

import config
import features
from features.context import Topology
import parser
import pathfinder
import renderer
import icon_library
import inventory
import drawio
import positions
import vsdx

EXIT_OUTPUT_FAILED = 3

# format name -> (file name, writer). Each writer takes (diagram, path).
WRITERS = {
    "vsdx": ("topology.vsdx", vsdx.write),
    "drawio": ("topology.drawio", drawio.write),
}


def _parse_path_arg(argv, flag, default):
    for a in argv:
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return default


def _place_unpinned_nodes(pinned, unpinned_names, sizes=None):
    """Simple staging-grid placement for any node with no position in
    the position template yet. No layout library involved - just somewhere
    visible below whatever's already pinned, meant to be dragged into
    place by hand on the next pass rather than auto-arranged well.

    The grid's pitch grows to fit the largest staged node plus
    NEW_NODE_GRID_MIN_GAP, so staged nodes never overlap (overlapping nodes
    can wall a link's port in, and the router then finds no route).
    """
    sizes = sizes or {}
    widest = max((sizes.get(n, (config.DEFAULT_NODE_W, 0))[0] for n in unpinned_names),
                 default=config.DEFAULT_NODE_W)
    tallest = max((sizes.get(n, (0, config.DEFAULT_NODE_H))[1] for n in unpinned_names),
                  default=config.DEFAULT_NODE_H)
    pitch_x = max(config.NEW_NODE_GRID_SPACING_X, widest + config.NEW_NODE_GRID_MIN_GAP)
    pitch_y = max(config.NEW_NODE_GRID_SPACING_Y, tallest + config.NEW_NODE_GRID_MIN_GAP)
    if pinned:
        bottom = max(p["y"] for p in pinned.values()) + config.DEFAULT_NODE_H / 2
        left = min(p["x"] for p in pinned.values()) - config.DEFAULT_NODE_W / 2
    else:
        bottom, left = 0, 0
    start_y = bottom + config.NEW_NODE_GRID_TOP_MARGIN

    positions = {}
    for i, name in enumerate(unpinned_names):
        col, row = i % config.NEW_NODE_GRID_COLS, i // config.NEW_NODE_GRID_COLS
        cx = left + col * pitch_x + config.DEFAULT_NODE_W / 2
        cy = start_y + row * pitch_y + config.DEFAULT_NODE_H / 2
        positions[name] = (cx, cy)
    return positions


def _place_segments(segments, centers, sizes):
    """Put each unpinned segment node between its members (the centroid of
    their centres), nudged down until it overlaps no other node."""
    placed = {}
    for seg in segments:
        sid = seg["id"]
        if sid in centers:
            continue
        xs = [centers[m][0] for m in seg["members"]]
        ys = [centers[m][1] for m in seg["members"]]
        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
        w, h = sizes[sid]
        for _ in range(50):
            clash = any(abs(cx - ox) < (w + sizes[o][0]) / 2 + 10 and
                        abs(cy - oy) < (h + sizes[o][1]) / 2 + 10
                        for o, (ox, oy) in {**centers, **placed}.items())
            if not clash:
                break
            cy += h + 20
        placed[sid] = (cx, cy)
    return placed


def _load_node_sizes(inventory_path, node_names):
    """Host-specific 'w' and 'h' dimensions from inventory.yml (group vars
    inherited). Falls back to config defaults if not set, or for every
    node if the inventory can't be read.
    """
    try:
        all_hosts = inventory.host_vars(inventory_path)
    except (OSError, ValueError) as e:
        print(f"Note: couldn't read node sizes from {inventory_path} ({e}) - using defaults.")
        all_hosts = {}

    sizes = {}
    for name in node_names:
        hvars = all_hosts.get(name, {})
        w, h = hvars.get("w"), hvars.get("h")
        sizes[name] = (
            int(w) if w is not None else config.DEFAULT_NODE_W,
            int(h) if h is not None else config.DEFAULT_NODE_H,
        )
    return sizes


def main():
    argv = sys.argv[1:]
    positional = [a for a in argv if not a.startswith("--")]
    facts_dir = positional[0] if len(positional) > 0 else config.DEFAULT_FACTS_DIR
    out_dir = positional[1] if len(positional) > 1 else config.DEFAULT_OUTPUT_DIR
    inventory_path = _parse_path_arg(argv, "--inventory", config.DEFAULT_INVENTORY_PATH)
    template_path = _parse_path_arg(argv, "--template", config.DEFAULT_TEMPLATE_PATH)
    icon_library_path = _parse_path_arg(argv, "--icon-library", config.DEFAULT_ICON_LIBRARY_PATH)

    nodes = parser.load_nodes(facts_dir, reparse="--reparse" in argv)
    if not nodes:
        sys.exit(f"No fact files found in {facts_dir} - run the playbook first.")

    edges, segments = parser.extract_links(nodes)
    edges_by_id = {e["id"]: e for e in edges}
    node_names = [n["device"]["name"] for n in nodes]
    segment_ids = [s["id"] for s in segments]

    pinned = positions.load(template_path, inventory_path)
    pinned = {k: v for k, v in pinned.items()  # drop segments that no longer exist
              if not k.startswith(parser.SEGMENT_PREFIX) or k in segment_ids}

    # Dynamically evaluate sizes from inventory.yml before falling back to defaults
    sizes = _load_node_sizes(inventory_path, node_names)

    unpinned = [name for name in node_names if name not in pinned]
    new_positions = _place_unpinned_nodes(pinned, unpinned, sizes) if unpinned else {}
    if unpinned:
        print(f"Note: {len(unpinned)} node(s) have no position in the template yet, "
              f"placed in a staging grid: {', '.join(unpinned)}")

    centers = {}
    for name in node_names + segment_ids:
        if name in pinned:
            centers[name] = (pinned[name]["x"], pinned[name]["y"])
        elif name in new_positions:
            centers[name] = new_positions[name]

    sizes.update({sid: (config.SEGMENT_NODE_W, config.SEGMENT_NODE_H) for sid in segment_ids})
    centers.update(_place_segments(segments, centers, sizes))
    preferred_sides = {name: p["side"] for name, p in pinned.items() if "side" in p}

    hanan_nodes, specs = pathfinder.build_link_specs(edges, centers, sizes, preferred_sides)

    icons_by_id = parser.load_icons(inventory_path)
    icon_lib = icon_library.load_icon_library(icon_library_path)
    diagram = renderer.build(hanan_nodes, specs, edges_by_id, icons_by_id, icon_lib, segments)

    # Protocol features (features/), each on its own layer above the
    # physical diagram. A feature that fails is reported and left out.
    topo = Topology(nodes, edges, hanan_nodes, specs, segments)
    feature_results = features.run_features(diagram.pages[0], topo)

    seg_note = f" and {len(segments)} shared segment(s)" if segments else ""
    print(f"\nDrew {len(nodes)} nodes{seg_note} and {len(edges)} links")
    for name, status in feature_results.items():
        print(f"  feature {name}: {status}")

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    failed = write_outputs(diagram, out_dir, _formats(argv))

    # Regenerate the position template from this run's final node
    # positions/sizes, so it always matches the current inventory and
    # carries forward whatever position/side each node already had.
    page = diagram.pages[0]
    try:
        written = positions.write_template(template_path, [page.get(n.id) for n in hanan_nodes],
                                           preferred_sides)
        print(f"Wrote position template to {written}")
    except Exception as e:  # noqa: BLE001 - keep the old template, report, exit 3
        print(f"ERROR: the position template couldn't be written "
              f"({positions.output_path(template_path)} is unchanged): {e}")
        failed.append("template")
    if failed:
        sys.exit(EXIT_OUTPUT_FAILED)


def _formats(argv) -> list:
    value = next((a.split("=", 1)[1] for a in argv if a.startswith("--formats=")), None)
    formats = ([f.strip() for f in value.split(",") if f.strip()] if value is not None
               else list(config.OUTPUT_FORMATS))
    unknown = [f for f in formats if f not in WRITERS]
    if unknown:
        sys.exit(f"Unknown output format(s): {', '.join(unknown)} "
                 f"(choose from {', '.join(WRITERS)})")
    return formats


def write_outputs(diagram, out_dir, formats) -> list:
    """Write the diagram in each format. Each writer is independent: one
    that fails is reported and its file deleted (never a stale or partial
    file), and the rest are still written. Returns the formats that failed."""
    failed = []
    for fmt, (name, writer) in WRITERS.items():
        path = Path(out_dir) / name
        if fmt not in formats:
            path.unlink(missing_ok=True)   # don't leave an older run's file behind
            continue
        try:
            writer(diagram, path)
        except Exception as e:
            failed.append(fmt)
            path.unlink(missing_ok=True)
            print(f"ERROR: {name} couldn't be written: {type(e).__name__}: {e}")
            continue
        print(f"Wrote {path}")
    return failed


if __name__ == "__main__":
    main()
