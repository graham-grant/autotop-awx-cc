"""
Cisco IOS / IOS-XE driver: reads show-command output into the schema 2
core (device, interfaces, neighbors, arp, mac_table) and the facets this
platform can supply (ipsec, vlans).

Commands a box doesn't support (no LLDP on a router, no MAC table on a
C8000v) are skipped at collection, and their parsers just return nothing.
"""
from __future__ import annotations

import re

from drivers.common import canonical_intf, norm_mac

TRANSPORT = "ssh-cli"
VENDOR = "cisco"

CORE_COMMANDS = ["show version", "show interfaces", "show ip interface",
                 "show lldp neighbors detail", "show cdp neighbors detail",
                 "show ip arp", "show mac address-table dynamic"]
PAGING_COMMANDS = ["terminal length 0"]
# Run before and after collection: the "! Last configuration change at ...
# by ..." line from the running config. If it moves while we are logged in,
# collection fails loudly (see collect.py and scripts/readonly.py).
CONFIG_CHECK_COMMAND = "show running-config | include configuration change"
# facet -> commands it is parsed from. Gathered only when a feature that
# draws the facet is enabled.
FACET_COMMANDS = {"ipsec": ["show crypto session detail"],
                  "vlans": ["show vlan brief", "show interfaces trunk"]}

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
_MEMBERS = re.compile(r"Members in this channel:\s*(.+)")
_DOTTED_MAC = r"[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}"
_ARP_LINE = re.compile(r"^Internet\s+(\d+\.\d+\.\d+\.\d+)\s+\S+\s+(" + _DOTTED_MAC +
                       r")\s+\S+\s*(\S*)\s*$")
_MAC_LINE = re.compile(r"^\s*(\d+|All)\s+(" + _DOTTED_MAC + r")\s+\S+\s+(?:\S+\s+)*?"
                       r"([A-Za-z][\w./:-]*\d)\s*$")
_VLAN_LINE = re.compile(r"^(\d+)\s+(\S+)\s+(active|act/\S+|suspended|sus\S*)\s*(.*)$")
_CDP_CAPS = {"router": "router", "switch": "bridge", "trans-bridge": "bridge",
             "source-route-bridge": "bridge", "host": "station", "phone": "phone"}
_LLDP_CAPS = {"R": "router", "B": "bridge", "T": "phone", "W": "wlan", "S": "station",
              "C": "docsis", "O": "other", "P": "repeater"}
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
        members = _MEMBERS.search(body)
        if kind == "lag" and members:
            interfaces[name]["_members"] = members.group(1).split()
    # LAG members point at their port-channel
    for name, intf in interfaces.items():
        for member in intf.pop("_members", []):
            target = interfaces.get(canonical_intf(member, interfaces))
            if target is not None:
                target["parent"] = name
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


def _caps(words, table) -> list[str]:
    found = []
    for w in words:
        cap = table.get(w if table is _LLDP_CAPS else w.lower())
        if cap and cap not in found:
            found.append(cap)
    return found


def _neighbor(protocol, local, remote_name, remote_intf=None, remote_mac=None,
              remote_ip=None, platform=None, caps=()) -> dict:
    return {"local_intf": local, "protocol": protocol, "remote_name": remote_name,
            "remote_intf": remote_intf, "remote_mac": remote_mac, "remote_ip": remote_ip,
            "remote_platform": platform, "remote_capabilities": list(caps)}


def _entries(text: str, sep: str):
    block = []
    for line in (text or "").splitlines():
        if line.strip().startswith(sep) and set(line.strip()) == {"-"}:
            if block:
                yield block
            block = []
        else:
            block.append(line.rstrip())
    if block:
        yield block


def parse_cdp_neighbors(text: str) -> list[dict]:
    """`show cdp neighbors detail` -> neighbor records."""
    out = []
    for block in _entries(text, "---"):
        f = {}
        for line in block:
            s = line.strip()
            if s.startswith("Device ID:"):
                f["name"] = re.sub(r"\(.*\)$", "", s.split(":", 1)[1].strip())
            elif s.startswith("IP address:") and "ip" not in f:
                f["ip"] = s.split(":", 1)[1].strip()
            elif s.startswith("Platform:"):
                plat, _, caps = s.partition(",")
                f["platform"] = plat.split(":", 1)[1].strip()
                f["caps"] = caps.split(":", 1)[1].split() if ":" in caps else []
            elif s.startswith("Interface:"):
                local, _, port = s.partition(",")
                f["local"] = local.split(":", 1)[1].strip()
                f["port"] = port.split(":", 1)[1].strip() if ":" in port else None
        if f.get("name") and f.get("local"):
            out.append(_neighbor("cdp", f["local"], f["name"], f.get("port"), None,
                                 f.get("ip"), f.get("platform"),
                                 _caps(f.get("caps", []), _CDP_CAPS)))
    return out


def parse_lldp_neighbors(text: str) -> list[dict]:
    """`show lldp neighbors detail` -> neighbor records. A server's port id
    is usually its NIC MAC, which then becomes remote_mac."""
    out = []
    for block in _entries(text, "---"):
        f = {}
        for line in block:
            s = line.strip()
            key, _, value = s.partition(":")
            value = value.strip()
            if key == "Local Intf":
                f["local"] = value
            elif key == "Chassis id":
                f["chassis"] = value
            elif key == "Port id":
                f["port"] = value
            elif key == "Port Description":
                f["port_desc"] = value
            elif key == "System Name":
                f["name"] = value
            elif key == "Enabled Capabilities":
                f["caps"] = [c.strip() for c in value.split(",") if c.strip()]
            elif key == "IP" and "ip" not in f:
                f["ip"] = value
        if not f.get("local"):
            continue
        port_mac = norm_mac(f.get("port"))
        remote_intf = f.get("port_desc") if port_mac else f.get("port")
        name = f.get("name") or None
        out.append(_neighbor("lldp", f["local"], name, remote_intf or None,
                             port_mac or norm_mac(f.get("chassis")), f.get("ip"), None,
                             _caps(f.get("caps", []), _LLDP_CAPS)))
    return out


def parse_arp(text: str) -> list[dict]:
    out = []
    for line in (text or "").splitlines():
        m = _ARP_LINE.match(line.strip())
        if m:
            out.append({"ip": m.group(1), "mac": norm_mac(m.group(2)),
                        "intf": m.group(3) or None, "vrf": "default"})
    return out


def parse_mac_table(text: str) -> list[dict]:
    """`show mac address-table dynamic` -> {mac, intf, vlan}. CPU, router and
    drop entries are left out."""
    out = []
    for line in (text or "").splitlines():
        m = _MAC_LINE.match(line)
        if not m or m.group(3).upper() in ("CPU", "DROP", "ROUTER", "SWITCH"):
            continue
        out.append({"mac": norm_mac(m.group(2)), "intf": m.group(3),
                    "vlan": int(m.group(1)) if m.group(1).isdigit() else None})
    return out


def parse_vlan_brief(text: str) -> list[dict]:
    """`show vlan brief` -> [{id, name, ports}]. The reserved FDDI/Token Ring
    VLANs (1002-1005) are left out."""
    vlans, current = [], None
    for line in (text or "").splitlines():
        m = _VLAN_LINE.match(line)
        if m:
            current = {"id": int(m.group(1)), "name": m.group(2),
                       "ports": [p for p in re.split(r"[,\s]+", m.group(4)) if p]}
            if not 1002 <= current["id"] <= 1005:
                vlans.append(current)
            continue
        if current is not None and line.startswith(" ") and line.strip():
            current["ports"] += [p for p in re.split(r"[,\s]+", line.strip()) if p]
    return vlans


def parse_interfaces_trunk(text: str) -> list[dict]:
    """`show interfaces trunk` -> [{port, native, allowed, active}], the VLAN
    lists kept as range strings ("1-4094", "1,10,20"), or "" for none."""
    trunks: dict[str, dict] = {}
    section = None
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("Port"):
            section = ("mode" if "Native" in s else "active" if "active in" in s
                       else "allowed" if "allowed on trunk" in s else None)
            continue
        port, _, rest = s.partition(" ")
        rest = rest.strip()
        if section == "mode":
            native = rest.split()[-1] if rest else None
            trunks[port] = {"port": port, "native": int(native) if native and native.isdigit()
                            else None, "allowed": "", "active": ""}
        elif section in ("allowed", "active") and port in trunks:
            trunks[port][section] = "" if rest.lower() == "none" else rest.replace(" ", "")
    return list(trunks.values())


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
    names = list(interfaces)
    neighbors = (parse_lldp_neighbors(raw.get("show lldp neighbors detail", ""))
                 + parse_cdp_neighbors(raw.get("show cdp neighbors detail", "")))
    for n in neighbors:
        n["local_intf"] = canonical_intf(n["local_intf"], names)
    arp = parse_arp(raw.get("show ip arp", ""))
    for a in arp:
        a["intf"] = canonical_intf(a["intf"], names)
        a["vrf"] = (interfaces.get(a["intf"]) or {}).get("vrf", "default")
    mac_table = parse_mac_table(raw.get("show mac address-table dynamic", ""))
    for entry in mac_table:
        entry["intf"] = canonical_intf(entry["intf"], names)
    return {
        "device": {"hostname": version["hostname"], "role": version["role"],
                   "vendor": VENDOR, "model": version["model"],
                   "os_version": version["os_version"], "serial": version["serial"]},
        "interfaces": list(interfaces.values()),
        "neighbors": neighbors, "arp": arp, "mac_table": mac_table,
    }


def parse_facets(raw: dict) -> dict:
    facets = {}
    text = raw.get("show crypto session detail")
    if text is not None:
        facets["ipsec"] = {"tunnels": parse_crypto_session(text)}
    if raw.get("show vlan brief") is not None:
        names = list(parse_show_interfaces(raw.get("show interfaces", "")))
        vlans = parse_vlan_brief(raw["show vlan brief"])
        for v in vlans:
            v["ports"] = [canonical_intf(p, names) for p in v["ports"]]
        trunks = parse_interfaces_trunk(raw.get("show interfaces trunk", ""))
        for t in trunks:
            t["port"] = canonical_intf(t["port"], names)
        facets["vlans"] = {"vlans": vlans, "trunks": trunks}
    return facets
