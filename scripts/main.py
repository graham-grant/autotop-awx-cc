#!/usr/bin/env python3
"""
Build a drawio topology diagram from the per-host JSON files written by
playbooks/gather_topology.yml.

This is the coordinator: load data (parser.py), decide node positions and
route every link through the Hanan-grid A* engine (pathfinder.py), then
emit draw.io XML with IP/interface labels and status dots (renderer.py),
then let each enabled protocol feature (features/ - e.g. IPsec tunnels)
draw itself onto its own layer. Tunable settings live in config.py.

Runs on a base Python 3 install: no third-party packages. Host JSON is raw
show-command output normalized by netparse/, inventory.yml is read by
miniyaml.py, and the A* router's search runs in a C extension that ships
prebuilt (_hanan.abi3.so, CPython 3.9+ stable ABI) - no compiler needed.

Links are derived from directly-connected subnets: every interface IP
the device reports is grouped by network, and any subnet shared by exactly two
inventory hosts becomes a link between them. A subnet shared by three or
more hosts is drawn as a full mesh, with a note printed.

Node positions are no longer kept in a YAML config - they're read straight
out of template.drawio (drawio_position_loader.py), a stripped-down
companion file containing just one rectangle per inventory host, no links
or labels. Reposition nodes by opening that file, dragging them around,
and saving - the next run picks the new positions straight up. A node can
also get a preferred attachment side (top/bottom/left/right), overriding
the automatic per-node majority-vote choice, by adding a custom "side"
property to it in draw.io (right-click -> Edit Style, or the Edit Data
panel) - this round-trips the same way positions do.

template.drawio itself is regenerated every run to match whatever's
currently in inventory.yml: existing nodes keep whatever position you last
left them at, new nodes appear in a simple staging grid below the rest,
ready to be dragged into place. There's no positioning library involved
(N2G has been removed) - an unplaced node just needs to be visible
somewhere reasonable, not laid out well, since you're expected to
reposition it by hand once.

Usage:
    python3 main.py [facts_dir] [output_dir] [--inventory PATH] [--template PATH] [--icon-library PATH]

Defaults: facts_dir=output/facts, output_dir=output,
inventory=<project root>/inventory.yml, template=<project root>/template.drawio,
icon-library=<project root>/docs/Prod_Library.xml

Each host can get a preferred icon by adding an `icon:` key to its entry
in inventory.yml (or a `vars: {icon: ...}` block on a group, inherited by
every host under it) - the value must match an entry's title in the icon
library exactly, e.g. icon: "C9200-24P Front". A host with no icon set
falls back to the plain rectangle.
"""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import config
import features
from features.context import Topology
import parser
import pathfinder
import renderer
import icon_library
import inventory
import drawio_position_loader as dpl


def _parse_path_arg(argv, flag, default):
    for a in argv:
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return default


def _place_unpinned_nodes(pinned, unpinned_names):
    """Simple staging-grid placement for any node with no position in
    template.drawio yet. No layout library involved - just somewhere
    visible below whatever's already pinned, meant to be dragged into
    place by hand on the next pass rather than auto-arranged well.
    """
    if pinned:
        bottom = max(p["y"] for p in pinned.values()) + config.DEFAULT_NODE_H / 2
        left = min(p["x"] for p in pinned.values()) - config.DEFAULT_NODE_W / 2
    else:
        bottom, left = 0, 0
    start_y = bottom + config.NEW_NODE_GRID_TOP_MARGIN

    positions = {}
    for i, name in enumerate(unpinned_names):
        col, row = i % config.NEW_NODE_GRID_COLS, i // config.NEW_NODE_GRID_COLS
        cx = left + col * config.NEW_NODE_GRID_SPACING_X + config.DEFAULT_NODE_W / 2
        cy = start_y + row * config.NEW_NODE_GRID_SPACING_Y + config.DEFAULT_NODE_H / 2
        positions[name] = (cx, cy)
    return positions


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
    positional = [a for a in argv if "=" not in a]
    facts_dir = positional[0] if len(positional) > 0 else config.DEFAULT_FACTS_DIR
    out_dir = positional[1] if len(positional) > 1 else config.DEFAULT_OUTPUT_DIR
    inventory_path = _parse_path_arg(argv, "--inventory", config.DEFAULT_INVENTORY_PATH)
    template_path = _parse_path_arg(argv, "--template", config.DEFAULT_TEMPLATE_PATH)
    icon_library_path = _parse_path_arg(argv, "--icon-library", config.DEFAULT_ICON_LIBRARY_PATH)

    nodes = parser.load_nodes(facts_dir)
    if not nodes:
        sys.exit(f"No fact files found in {facts_dir} - run the playbook first.")

    edges = parser.extract_edges(nodes)
    edges_by_id = {e["id"]: e for e in edges}
    node_names = [n["inventory_name"] for n in nodes]

    pinned = dpl.load_pinned_positions_from_drawio(template_path, inventory_path)

    unpinned = [name for name in node_names if name not in pinned]
    new_positions = _place_unpinned_nodes(pinned, unpinned) if unpinned else {}
    if unpinned:
        print(f"Note: {len(unpinned)} node(s) have no position in template.drawio yet, "
              f"placed in a staging grid: {', '.join(unpinned)}")

    centers = {}
    for name in node_names:
        if name in pinned:
            centers[name] = (pinned[name]["x"], pinned[name]["y"])
        else:
            centers[name] = new_positions[name]

    # Dynamically evaluate sizes from inventory.yml before falling back to defaults
    sizes = _load_node_sizes(inventory_path, node_names)
    preferred_sides = {name: p["side"] for name, p in pinned.items() if "side" in p}

    hanan_nodes, specs = pathfinder.build_link_specs(edges, centers, sizes, preferred_sides)

    icons_by_id = parser.load_icons(inventory_path)
    icon_lib = icon_library.load_icon_library(icon_library_path)
    mxfile = renderer.render_tree(hanan_nodes, specs, edges_by_id, icons_by_id, icon_lib)

    # Protocol features (features/), each on its own draw.io layer above the
    # physical diagram. A feature that fails is reported and left out.
    topo = Topology(nodes, edges, hanan_nodes, specs)
    feature_results = features.run_features(mxfile.find(".//root"), topo)
    xml_str = ET.tostring(mxfile, encoding="unicode")

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    out_path = Path(out_dir) / "topology.drawio"
    out_path.write_text(xml_str)
    print(f"\nWrote {len(nodes)} nodes and {len(edges)} links to {out_path}")
    for name, status in feature_results.items():
        print(f"  feature {name}: {status}")

    # Regenerate template.drawio from this run's final node positions/sizes,
    # so it always matches the current inventory and carries forward
    # whatever position/side each node already had.
    final_positions = {n.id: (n.x, n.y, n.w, n.h) for n in hanan_nodes}
    template_xml = dpl.build_template_xml(final_positions, preferred_sides)
    Path(template_path).write_text(template_xml)
    print(f"Wrote position template to {template_path}")


if __name__ == "__main__":
    main()
