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
# inventory.yml and template.drawio live at the project root, one level up
# from scripts/ - sibling to the playbooks/ directory.
_PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_ICON_LIBRARY_PATH = _PROJECT_ROOT / "docs/Prod_Library.xml"
DEFAULT_INVENTORY_PATH = _PROJECT_ROOT / "inventory.yml"
DEFAULT_TEMPLATE_PATH = _PROJECT_ROOT / "template.drawio"

# Fallback grid placement for a node with no position in template.drawio
# yet (new device, or template.drawio doesn't exist at all). No layout
# library involved - just a staging area below whatever's already pinned,
# so a new node is visible somewhere reasonable and easy to drag into
# place, rather than literally requiring a layout algorithm to guess.
NEW_NODE_GRID_COLS = 5
NEW_NODE_GRID_SPACING_X = 200
NEW_NODE_GRID_SPACING_Y = 120
NEW_NODE_GRID_TOP_MARGIN = 100  # gap below the lowest existing node

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
# Status dot styling
# ---------------------------------------------------------------------------
STATUS_UP_COLOR = "#00CC00"
STATUS_DOWN_COLOR = "#CC0000"
STATUS_DOT_SIZE = 14

# ---------------------------------------------------------------------------
# Label styling
# ---------------------------------------------------------------------------
IP_LABEL_FRACTION = 0.5    # how far in from each end (0-1 along the link) the IP label sits
INTF_LABEL_FRACTION = 0.85  # how close to each end the rotated interface-id label sits
INTF_LABEL_STYLE = "text;html=1;align=center;verticalAlign=middle;rotation=90;fontSize=9;"
IP_LABEL_STYLE = "labelBackgroundColor=#ffffff;fontSize=10;"

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
# Shared segments: a subnet with three or more inventory members is drawn as
# a small node of its own, linked to each member, instead of a full mesh.
# Its position can be pinned in template.drawio like any node (its label
# there starts with "segment "); otherwise it's placed between its members.
# ---------------------------------------------------------------------------
SEGMENT_NODE_W = 160
SEGMENT_NODE_H = 30
SEGMENT_NODE_STYLE = ("rounded=1;arcSize=50;whiteSpace=wrap;html=1;fontSize=10;"
                      "fillColor=#f5f5f5;strokeColor=#666666;fontColor=#333333;")

# Zero-width space: a plain "" or " " label on a drawio vertex falls back to
# showing the cell's id instead of staying blank. U+200B isn't treated as
# whitespace, so it survives and renders as an effectively invisible label.
BLANK_LABEL = "\u200b"

# ---------------------------------------------------------------------------
# Feature plugins (scripts/features/) - drawn on their own draw.io layers
# on top of the physical diagram. Order here = layer stacking order.
# None enables every feature module found.
# ---------------------------------------------------------------------------
ENABLED_FEATURES = ["ipsec"]

# Per-feature overrides of each feature's SETTINGS defaults, e.g.
#   FEATURE_SETTINGS = {"ipsec": {"up_color": "#0050EF", "lane_spacing": 12}}
FEATURE_SETTINGS = {}
