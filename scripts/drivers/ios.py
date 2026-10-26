"""
Cisco IOS / IOS-XE driver: reads show-command output into the schema 2
core (device, interfaces, neighbors, arp, mac_table) and the facets this
platform can supply (ipsec).
"""
from __future__ import annotations

import re

TRANSPORT = "ssh-cli"
VENDOR = "cisco"

CORE_COMMANDS = ["show version", "show interfaces", "show ip interface"]
PAGING_COMMANDS = ["terminal length 0"]
# Run before and after collection: the "! Last configuration change at ...
# by ..." line from the running config. If it moves while we are logged in,
# collection fails loudly (see collect.py and scripts/readonly.py).
CONFIG_CHECK_COMMAND = "show running-config | include configuration change"
# facet -> commands it is parsed from. Gathered only when a feature that
# draws the facet is enabled.
FACET_COMMANDS = {"ipsec": ["show crypto session detail"]}

# "GigabitEthernet0/1 is up, line protocol is up (connected)"
# "Vlan10 is administratively down, line protocol is down"
_INTF_HEADER = re.compile(
    r"^(?P<name>\S+) is (?P<admin>administratively down|up|down|deleted)\S*,"
    r"\s+line protocol is (?P<proto>\w+)"
)
_MAC = re.compile(r"address is ([0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4})")
_MTU = re.compile(r"MTU (\d+) bytes")
_BW = re.compile(r"BW (\d+) Kbit")
_DOT1Q = re.compile(r"Encapsulation 802\.1Q Virtual LAN, Vlan ID\s+(\d+)")
_IP_PRIMARY = re.compile(r"Internet address is (\d+\.\d+\.\d+\.\d+)/(\d+)")
_IP_SECONDARY = re.compile(r"Secondary address (\d+\.\d+\.\d+\.\d+)/(\d+)")
_VRF = re.compile(r'VPN Routing/Forwarding "([^"]+)"')
_HOSTNAME = re.compile(r"^(\S+) uptime is", re.MULTILINE)
_VERSION = re.compile(r"^Cisco IOS.*?Version ([^\s,]+)", re.MULTILINE)
_MODEL = re.compile(r"^cisco (\S+)(?: \([^)]*\))? .*?processor", re.MULTILINE | re.IGNORECASE)
_SERIAL = re.compile(r"^Processor board ID (\S+)", re.MULTILINE)
_SA_RE = re.compile(r"IKEv[12] SA:\s+local\s+([0-9a-fA-F:.]+)/\d+\s+remote\s+([0-9a-fA-F:.]+)/\d+")
_FLOW_RE = re.compile(r"IPSEC FLOW:.*?host\s+([0-9a-fA-F:.]+)\s+host\s+([0-9a-fA-F:.]+)")
# Models that are switches rather than routers (for the default icon/role)
_SWITCH_MODEL = re.compile(r"^(WS-C|C9[2-6]\d\d|C1000|C2960|C3[68]50|IE-)", re.IGNORECASE)

_KINDS = (("Loopback", "loopback"), ("Tunnel", "tunnel"), ("Port-channel", "lag"),
          ("Vlan", "vlan"), ("BDI", "vlan"), ("NVI", "virtual"), ("Null", "virtual"),
          ("Virtual", "virtual"))


def _mac(dotted: str) -> str:
    """'5254.0012.3456' -> '52:54:00:12:34:56'."""
    h = dotted.replace(".", "").lower()
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


def _blocks(text: str):
    """Split output into (header match, block lines) per interface."""
    header, lines = None, []
    for line in (text or "").splitlines():
        m = _INTF_HEADER.match(line)
        if m:
            if header:
                yield header, lines
            header, lines = m, []
        elif header:
            lines.append(line.strip())
    if header:
        yield header, lines


def _kind_and_parent(name: str) -> tuple[str, str | None]:
    if "." in name:
        return "subinterface", name.split(".", 1)[0]
    for prefix, kind in _KINDS:
        if name.startswith(prefix):
            return kind, None
    return "physical", None


def parse_show_version(text: str) -> dict:
    text = text or ""
    found = {}
    for key, rx in (("hostname", _HOSTNAME), ("os_version", _VERSION),
                    ("model", _MODEL), ("serial", _SERIAL)):
        m = rx.search(text)
        found[key] = m.group(1) if m else None
    model = found["model"] or ""
    found["role"] = "switch" if _SWITCH_MODEL.match(model) else "router"
    return found


def parse_show_interfaces(text: str) -> dict:
    """{name: interface fields} in output order."""
    interfaces = {}
    for header, lines in _blocks(text):
        name = header["name"]
        body = "\n".join(lines)
        description = ""
        for l in lines:
            if l.startswith("Description:"):
                description = l.split(":", 1)[1].strip()
        mac, mtu, bw, dot1q = _MAC.search(body), _MTU.search(body), _BW.search(body), _DOT1Q.search(body)
        kind, parent = _kind_and_parent(name)
        vlan = int(dot1q.group(1)) if dot1q else None
        if vlan is None and kind == "vlan" and name[4:].isdigit():
            vlan = int(name[4:])
        speed = int(bw.group(1)) / 1000 if bw else None  # Kbit/s -> Mbit/s
        interfaces[name] = {
            "name": name, "kind": kind,
            "mac": _mac(mac.group(1)) if mac else None,
            "admin_up": header["admin"] != "administratively down",
            "oper_up": header["proto"] == "up",
            "speed_mbps": int(speed) if speed is not None and speed == int(speed) else speed,
            "mtu": int(mtu.group(1)) if mtu else None,
            "description": description,
            "vrf": "default", "vlan": vlan, "parent": parent,
            "ipv4": [], "ipv6": [],
        }
    return interfaces


def parse_show_ip_interface(text: str) -> dict:
    """{name: {"vrf": str, "ipv4": ["a.b.c.d/len", ...]}}, primary first."""
    result = {}
    for header, lines in _blocks(text):
        ipv4, vrf = [], "default"
        for l in lines:
            m = _IP_PRIMARY.search(l) or _IP_SECONDARY.search(l)
            if m:
                ipv4.append(f"{m.group(1)}/{m.group(2)}")
            elif (v := _VRF.search(l)):
                vrf = v.group(1)
        result[header["name"]] = {"vrf": vrf, "ipv4": ipv4}
    return result


def parse_crypto_session(text: str) -> list[dict]:
    """`show crypto session detail` -> ipsec facet tunnel records.

    Output is a series of blocks starting with "Interface: X"; each has a
    "Session status:" line, a "Peer: <addr> port ..." line and (when IKE
    is up) an "IKEv1/IKEv2 SA: local A/500 remote B/500" line giving the
    local address. A session with no IKE SA falls back to the IPSEC FLOW
    host pair, and failing that leaves local_address unset.

    "status" is the facet's normalised up/down/unknown; IOS's own word
    (UP-ACTIVE, UP-IDLE, UP-NO-IKE, DOWN-NEGOTIATING, ...) is kept in
    "vendor_status".
    """
    tunnels = []
    current = None

    def flush():
        if current and current.get("peer_address"):
            tunnels.append(current)

    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("Interface:"):
            flush()
            current = {"interface": s.split(":", 1)[1].strip(), "local_address": None,
                       "peer_address": None, "status": "unknown", "vendor_status": None}
        elif current is None:
            continue
        elif s.startswith("Session status:"):
            word = s.split(":", 1)[1].strip()
            current["vendor_status"] = word
            current["status"] = ("up" if word.upper().startswith("UP") else
                                 "down" if word.upper().startswith("DOWN") else "unknown")
        elif s.startswith("Peer:") and current["peer_address"] is None:
            current["peer_address"] = s.split()[1]
        elif (m := _SA_RE.search(s)) and current["local_address"] is None:
            current["local_address"] = m.group(1)
            current["peer_address"] = current["peer_address"] or m.group(2)
        elif (m := _FLOW_RE.search(s)) and current["local_address"] is None:
            if m.group(2) == current["peer_address"]:
                current["local_address"] = m.group(1)
    flush()
    return tunnels



def parse_core(raw: dict) -> dict:
    version = parse_show_version(raw.get("show version", ""))
    interfaces = parse_show_interfaces(raw.get("show interfaces", ""))
    for name, ip in parse_show_ip_interface(raw.get("show ip interface", "")).items():
        intf = interfaces.get(name)
        if intf is None:  # in "show ip interface" only
            kind, parent = _kind_and_parent(name)
            intf = interfaces[name] = {
                "name": name, "kind": kind, "mac": None, "admin_up": None, "oper_up": None,
                "speed_mbps": None, "mtu": None, "description": "", "vrf": "default",
                "vlan": None, "parent": parent, "ipv4": [], "ipv6": []}
        intf["vrf"], intf["ipv4"] = ip["vrf"], ip["ipv4"]
    return {
        "device": {"hostname": version["hostname"], "role": version["role"],
                   "vendor": VENDOR, "model": version["model"],
                   "os_version": version["os_version"], "serial": version["serial"]},
        "interfaces": list(interfaces.values()),
        "neighbors": [], "arp": [], "mac_table": [],
    }


def parse_facets(raw: dict) -> dict:
    facets = {}
    text = raw.get("show crypto session detail")
    if text is not None:
        facets["ipsec"] = {"tunnels": parse_crypto_session(text)}
    return facets
