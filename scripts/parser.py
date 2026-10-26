"""
Reads the per-host facts files written by the playbook (schema 2, see
drivers/__init__.py; older files are upgraded on load) and derives links
with links.py (LLDP/CDP, MAC tables, shared subnets). Nothing here knows
about drawio, pathfinding, or rendering. Node positions are read from template.drawio by
drawio_position_loader.py.
"""
import json
from pathlib import Path

import drivers
import inventory
from links import SEGMENT_PREFIX, extract_links, get_is_up  # noqa: F401 (re-exported)


def load_icons(inventory_path):
    """Map each inventory host name to its preferred icon library title.

    Reads inventory.yml directly rather than through the facts-JSON
    pipeline above, since `icon` is an inventory property, not something
    NAPALM reports. Supports a `vars: {icon: ...}` block at any group
    level (inherited by every host under it) as well as a per-host
    `icon:` key, with the host-level value winning if both are set.
    Hosts with no icon set anywhere are simply absent from the result.
    """
    return {name: hv["icon"] for name, hv in inventory.host_vars(inventory_path).items()
            if hv.get("icon")}


def load_nodes(facts_dir, reparse=False):
    """Read every *.json file in facts_dir as a schema 2 record. With
    reparse=True, each record is re-parsed from its raw output with the
    current drivers (applies a parser fix to old files)."""
    nodes = []
    for path in sorted(Path(facts_dir).glob("*.json")):
        with open(path) as f:
            record = json.load(f)
        nodes.append(drivers.reparse(record) if reparse else drivers.load(record))
    return nodes
