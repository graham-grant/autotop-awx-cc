"""
Cisco IOS / IOS-XE show-command parsers, producing the same structures as
NAPALM's get_facts (hostname only), get_interfaces and get_interfaces_ip.
"""
from __future__ import annotations

import re

CORE_COMMANDS = ["show version", "show interfaces", "show ip interface"]

# "GigabitEthernet0/1 is up, line protocol is up (connected)"
# "Vlan10 is administratively down, line protocol is down"
_INTF_HEADER = re.compile(
    r"^(?P<name>\S+) is (?P<admin>administratively down|up|down|deleted)\S*,"
    r"\s+line protocol is (?P<proto>\w+)"
)
_MAC = re.compile(r"address is ([0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4})")
_MTU = re.compile(r"MTU (\d+) bytes")
_BW = re.compile(r"BW (\d+) Kbit")
_IP_PRIMARY = re.compile(r"Internet address is (\d+\.\d+\.\d+\.\d+)/(\d+)")
_IP_SECONDARY = re.compile(r"Secondary address (\d+\.\d+\.\d+\.\d+)/(\d+)")
_HOSTNAME = re.compile(r"^(\S+) uptime is", re.MULTILINE)


def _mac(dotted: str) -> str:
    """'5254.0012.3456' -> '52:54:00:12:34:56' (NAPALM's format)."""
    h = dotted.replace(".", "").upper()
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


def parse_show_version(text: str) -> str | None:
    m = _HOSTNAME.search(text or "")
    return m.group(1) if m else None


def parse_show_interfaces(text: str) -> dict:
    interfaces = {}
    for header, lines in _blocks(text):
        body = "\n".join(lines)
        description = ""
        for l in lines:
            if l.startswith("Description:"):
                description = l.split(":", 1)[1].strip()
        mac = _MAC.search(body)
        mtu = _MTU.search(body)
        bw = _BW.search(body)
        interfaces[header["name"]] = {
            "is_up": header["proto"] == "up",
            "is_enabled": header["admin"] != "administratively down",
            "description": description,
            "last_flapped": -1.0,
            "speed": float(bw.group(1)) / 1000 if bw else 0.0,  # Mbit/s, like NAPALM
            "mtu": int(mtu.group(1)) if mtu else 0,
            "mac_address": _mac(mac.group(1)) if mac else "",
        }
    return interfaces


def parse_show_ip_interface(text: str) -> dict:
    result = {}
    for header, lines in _blocks(text):
        ipv4 = {}
        for l in lines:
            m = _IP_PRIMARY.search(l) or _IP_SECONDARY.search(l)
            if m:
                ipv4[m.group(1)] = {"prefix_length": int(m.group(2))}
        if ipv4:
            result[header["name"]] = {"ipv4": ipv4}
    return result


def parse(cli: dict) -> dict:
    return {
        "hostname": parse_show_version(cli.get("show version", "")),
        "interfaces": parse_show_interfaces(cli.get("show interfaces", "")),
        "interfaces_ip": parse_show_ip_interface(cli.get("show ip interface", "")),
    }
