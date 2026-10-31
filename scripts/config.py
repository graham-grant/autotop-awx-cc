"""
Central configuration: paths, exclusion rules, styling, and Hanan-router
tuning constants. Nothing in here has side effects - other modules import
values from this file rather than redefining their own constants.
"""
import ipaddress
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DEFAULT_FACTS_DIR = "output/facts"
DEFAULT_OUTPUT_DIR = "output"
# inventory.yml and template.vsdx live at the project root, one level up
# from scripts/ - sibling to the playbooks/ directory.
_PROJECT_ROOT = Path(__file__).parent.parent
# Node icons: one PNG per icon, named as the inventory's `icon` values
DEFAULT_ICON_DIR = _PROJECT_ROOT / "icons"
DEFAULT_INVENTORY_PATH = _PROJECT_ROOT / "inventory.yml"
DEFAULT_TEMPLATE_PATH = _PROJECT_ROOT / "template.vsdx"

# The files main.py writes into the output directory: "vsdx" for
# topology.vsdx (Microsoft Visio, scripts/vsdx.py) and "drawio" for
# topology.drawio (scripts/drawio.py). Each format's writer works from the
# same diagram on its own, so either can be dropped. main.py --formats=...
# overrides this for one run.
OUTPUT_FORMATS = ["vsdx", "drawio"]

# Fallback grid placement for a node with no position in template.vsdx
# yet (new device, or template.vsdx doesn't exist at all). No layout
# library involved - just a staging area below whatever's already pinned,
# so a new node is visible somewhere reasonable and easy to drag into
# place, rather than literally requiring a layout algorithm to guess.
NEW_NODE_GRID_COLS = 5
NEW_NODE_GRID_SPACING_X = 200
NEW_NODE_GRID_SPACING_Y = 120
NEW_NODE_GRID_TOP_MARGIN = 100  # gap below the lowest existing node
NEW_NODE_GRID_MIN_GAP = 80      # the spacing grows so staged nodes are at least this far apart

# ---------------------------------------------------------------------------
# Link filtering
# ---------------------------------------------------------------------------
# Networks to ignore entirely when deriving links - e.g. end-host/access
# subnets that happen to be configured on a router interface but aren't a
# real network-element-to-network-element link. A more specific subnet
# within one of these (e.g. a /30 carved out of the /24) is excluded too.
EXCLUDED_NETWORKS = [
    ipaddress.ip_network("192.168.10.0/24"),
]

# ---------------------------------------------------------------------------
# Node sizing defaults (the Hanan router grows these further per node as
# needed to fit all of that node's ports - see pathfinder.ensure_node_capacity)
# ---------------------------------------------------------------------------
DEFAULT_NODE_W = 230
DEFAULT_NODE_H = 42

# ---------------------------------------------------------------------------
# Hanan-grid A* router tuning (see pathfinder.route_topology)
# ---------------------------------------------------------------------------
HANAN_BUFFER = 15.0          # minimum spacing between parallel lanes
HANAN_TURN_PENALTY = 500.0    # cost added per direction change, favors straighter runs
HANAN_MARGIN = 0.12          # inset from a node's corners when spacing ports along a side
HANAN_PERIMETER_MARGIN = None  # None = auto-derived from the busiest side, see pathfinder
HANAN_AFFINITY_PENALTY = 2.5 #penalize paths that travel alone in open space. removed if path is within buffer dist of other path. creates visual uniformity.
    #affinity_penalty = 1.5 (Recommended): Moving through empty space costs 2.5x more than riding an established bus lane. Lines will aggressively seek out others and cluster nicely.
    #affinity_penalty = 0.5: Lines will bundle if they are heading in roughly the same direction anyway, but won't take massive detours to find a bus lane.
    #affinity_penalty = 5.0+: Strict highways. Lines will trace out long, unnatural detours just to stay perfectly parallel with completely unrelated links across the map.

# ---------------------------------------------------------------------------
# Styles. Each is a diagram.py style dict (see the list of keys there), which
# both the draw.io and the Visio writer understand.
# ---------------------------------------------------------------------------
NODE_STYLE = {"wrap": True}                 # a device without an icon
LINK_STYLE = {"jumps": True}                # a link: hops over the links it crosses

# Interface labels at each end of a link. The background is the link status
# colour, and labels on top and bottom sides are turned to read bottom-up.
STATUS_UP_COLOR = "#00CC00"
STATUS_DOWN_COLOR = "#CC0000"
INTF_LABEL_STYLE = {"kind": "text", "font_size": 9, "font_color": "#000000",
                    "label_border": "#000000"}

# Common Cisco/Juniper/Arista interface name abbreviations. Names that don't
# match any of these (e.g. already-short Junos/Arista names like "ge-0/0/0"
# or "Eth1") pass through unchanged.
INTF_ABBREVIATIONS = [
    ("TenGigabitEthernet", "Te"), ("GigabitEthernet", "Gi"), ("FastEthernet", "Fa"),
    ("HundredGigE", "Hu"), ("FortyGigE", "Fo"), ("TwentyFiveGigE", "Twe"),
    ("Ethernet", "Eth"), ("Loopback", "Lo"), ("Port-channel", "Po"),
    ("Vlan", "Vl"), ("Tunnel", "Tu"),
]

# ---------------------------------------------------------------------------
# Link evidence (see links.py): which kinds of evidence may draw a link.
#   lldp, cdp  neighbor protocols - the exact cable
#   mac        a device's MAC learned on a switch access port (drawn dashed,
#              since it's inferred)
#   subnet     two or more devices with an address in the same subnet
# Remove "mac" to hide inferred server links, for example.
# ---------------------------------------------------------------------------
LINK_EVIDENCE = ("lldp", "cdp", "mac", "subnet")
INFERRED_LINK_STYLE = {"dashed": True, "dash": "4 4"}

# ---------------------------------------------------------------------------
# Shared segments: a subnet with three or more inventory members is drawn as
# a small node of its own, linked to each member, instead of a full mesh.
# Its position can be pinned in template.vsdx like any node (its text
# there starts with "segment "); otherwise it's placed between its members.
# ---------------------------------------------------------------------------
SEGMENT_NODE_W = 160
SEGMENT_NODE_H = 30
SEGMENT_NODE_STYLE = {"rounded": True, "arc": 50, "wrap": True, "font_size": 10,
                      "fill": "#f5f5f5", "stroke": "#666666", "font_color": "#333333"}

# ---------------------------------------------------------------------------
# Feature plugins (scripts/features/) - each drawn on its own layer on top
# of the physical diagram. Order here = layer stacking order.
# None enables every feature module found.
# ---------------------------------------------------------------------------
ENABLED_FEATURES = ["ipsec", "vlans", "zones", "roles"]

# Per-feature overrides of each feature's SETTINGS defaults, e.g.
#   FEATURE_SETTINGS = {"ipsec": {"up_color": "#0050EF", "lane_spacing": 12}}
FEATURE_SETTINGS = {}
