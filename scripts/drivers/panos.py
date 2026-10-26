"""
Palo Alto Networks PAN-OS driver: XML API responses -> the schema 2 core,
plus the ipsec and zones facets.

Requests are XML API query strings (see readonly.XmlApiPolicy and
scripts/xmlapi.py), and raw output is each response's XML, keyed by its
request. Responses are read with xml.etree; every field is optional, so a
PAN-OS version that leaves one out just leaves it empty.

Interfaces come from `show interface all`: its <ifnet> part has each
logical interface's zone, virtual router ("fwd": vr:<name>, which becomes
the interface's vrf) and address, and its <hw> part each port's MAC, state
and speed. Subinterfaces (ethernet1/4.20) take their VLAN from the tag.

Firewalls don't report an interface's admin state or MTU here, so those
are left None.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from drivers.common import norm_mac

TRANSPORT = "xml-api"
VENDOR = "paloalto"
PAGING_COMMANDS: list[str] = []   # the API doesn't page

SYSTEM_INFO = "type=op&cmd=<show><system><info></info></system></show>"
INTERFACES = "type=op&cmd=<show><interface>all</interface></show>"
ARP = "type=op&cmd=<show><arp><entry name='all'/></arp></show>"
LLDP = "type=op&cmd=<show><lldp><neighbors>all</neighbors></lldp></show>"
VPN_FLOW = "type=op&cmd=<show><vpn><flow></flow></vpn></show>"

CORE_COMMANDS = [SYSTEM_INFO, INTERFACES, ARP, LLDP]
FACET_COMMANDS = {
    "ipsec": [VPN_FLOW],
    "zones": [INTERFACES],
}

# The config-change marker: the newest commit job. A commit (from the GUI,
# CLI, API or Panorama) is the only way a firewall's running configuration
# changes, and every commit is a job. config_marker() picks it out, so
# other jobs finishing during collection don't count as a change.
CONFIG_CHECK_COMMAND = "type=op&cmd=<show><jobs><all></all></jobs></show>"
_COMMIT_JOBS = ("commit", "commitall", "commit-all", "commitandpush", "validate")

_KINDS = (("ethernet", "physical"), ("ae", "lag"), ("loopback", "loopback"),
          ("tunnel", "tunnel"), ("vlan", "vlan"))
_LLDP_CAPS = {"B": "bridge", "R": "router", "T": "phone", "W": "wlan", "S": "station",
              "bridge": "bridge", "router": "router", "telephone": "phone",
              "wlan-access-point": "wlan", "station-only": "station"}
_IPSEC_STATUS = {"active": "up", "init": "down", "inactive": "down", "down": "down"}


def _result(text: str | None) -> ET.Element | None:
    if not text:
        return None
    root = ET.fromstring(text)
    return root.find("result") if root.tag == "response" else root


def _text(el, path) -> str | None:
    if el is None:
        return None
    value = el.findtext(path)
    value = value.strip() if value else None
    return None if value in (None, "", "N/A", "ukn", "unknown") else value


def config_marker(output: str) -> str | None:
    """The newest commit job from `show jobs all`, as "id type enqueued", or
    "no commit jobs" if there are none."""
    result = _result(output)
    if result is None:
        return None
    commits = []
    for job in result.iter("job"):
        kind = (_text(job, "type") or "").lower()
        if kind in _COMMIT_JOBS and (_text(job, "id") or "").isdigit():
            commits.append((int(_text(job, "id")), _text(job, "type"), _text(job, "tenq")))
    if not commits:
        return "no commit jobs"
    job_id, kind, enqueued = max(commits)
    return f"commit job {job_id} {kind} {enqueued}"


def _kind_and_parent(name: str) -> tuple[str, str | None]:
    if "." in name and not name.startswith(("tunnel.", "loopback.", "vlan.")):
        return "subinterface", name.split(".", 1)[0]
    for prefix, kind in _KINDS:
        if name.startswith(prefix):
            return kind, None
    return "virtual", None


def parse_system_info(text: str) -> dict:
    system = (_result(text) or ET.Element("x")).find("system")
    model = _text(system, "model")
    return {"hostname": _text(system, "hostname") or _text(system, "devicename"),
            "model": model, "serial": _text(system, "serial"),
            "os_version": _text(system, "sw-version"), "role": "firewall"}


def parse_interfaces(text: str) -> list[dict]:
    result = _result(text)
    if result is None:
        return []
    hw = {}
    for e in result.findall("./hw/entry"):
        if _text(e, "name"):
            hw[_text(e, "name")] = e
    out, seen = [], set()
    logical = result.findall("./ifnet/entry")
    for e in logical + [h for n, h in hw.items() if n not in
                        {_text(x, "name") for x in logical}]:
        name = _text(e, "name")
        if not name or name in seen:
            continue
        seen.add(name)
        kind, parent = _kind_and_parent(name)
        port = hw.get(name) or hw.get(parent or "")
        state = _text(port, "state")
        speed = _text(port, "speed")
        fwd = _text(e, "fwd") or ""
        tag = _text(e, "tag")
        ipv4 = [a for a in [_text(e, "ip")] + [(m.text or "").strip() for m in e.findall("./addr/member")]
                if a and re.fullmatch(r"\d+\.\d+\.\d+\.\d+/\d+", a)]
        ipv6 = [(m.text or "").strip() for m in e.findall("./addr6/member") if m.text]
        out.append({
            "name": name, "kind": kind, "mac": norm_mac(_text(hw.get(name), "mac")),
            "admin_up": None, "oper_up": None if state is None else state == "up",
            "speed_mbps": int(speed) if speed and speed.isdigit() else None, "mtu": None,
            "description": "", "vrf": fwd[3:] if fwd.startswith("vr:") else None,
            "vlan": int(tag) if tag and tag.isdigit() and tag != "0" else None,
            "parent": parent, "ipv4": list(dict.fromkeys(ipv4)), "ipv6": ipv6,
        })
    return out


def parse_arp(text: str, vrf_of=None) -> list[dict]:
    result = _result(text)
    out = []
    for e in (result.findall("./entries/entry") if result is not None else []):
        mac, ip = norm_mac(_text(e, "mac")), _text(e, "ip")
        if not mac or not ip:
            continue  # incomplete
        intf = _text(e, "interface")
        out.append({"ip": ip, "mac": mac, "intf": intf,
                    "vrf": (vrf_of or {}).get(intf) or "default"})
    return out


def parse_lldp(text: str) -> list[dict]:
    result = _result(text)
    out = []
    for local in (result.findall("entry") if result is not None else []):
        local_intf = local.get("name") or _text(local, "name")
        for n in local.findall("./neighbors/entry"):
            port_id = _text(n, "port-id")
            port_mac = norm_mac(port_id)
            mgmt = [m.get("name") or _text(m, "address")
                    for m in n.findall("./management-address/entry")]
            mgmt = [m for m in mgmt if m and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", m)]
            caps = []
            for c in re.split(r"[,\s]+", _text(n, "enabled-capabilities") or ""):
                cap = _LLDP_CAPS.get(c) or _LLDP_CAPS.get(c.lower())
                if cap and cap not in caps:
                    caps.append(cap)
            out.append({
                "local_intf": local_intf, "protocol": "lldp",
                "remote_name": _text(n, "system-name"),
                "remote_intf": _text(n, "port-description") if port_mac else port_id,
                "remote_mac": port_mac or norm_mac(_text(n, "chassis-id")),
                "remote_ip": mgmt[0] if mgmt else None,
                "remote_platform": None, "remote_capabilities": caps,
            })
    return out


def parse_vpn_flow(text: str) -> list[dict]:
    """`show vpn flow` -> ipsec facet tunnels. PAN-OS says "active" for an
    established tunnel and "init" for one still negotiating."""
    result = _result(text)
    tunnels = []
    for e in (result.findall("./IPSec/entry") if result is not None else []):
        word = _text(e, "state")
        tunnels.append({
            "interface": _text(e, "inner-if"), "local_address": _text(e, "localip"),
            "peer_address": _text(e, "peerip"), "name": _text(e, "name"),
            "status": _IPSEC_STATUS.get((word or "").lower(), "unknown"),
            "vendor_status": word,
        })
    return tunnels


def parse_zones(text: str) -> list[dict]:
    """Zones and their interfaces, from the <ifnet> part of `show interface
    all`."""
    result = _result(text)
    zones: dict[tuple, dict] = {}
    for e in (result.findall("./ifnet/entry") if result is not None else []):
        zone, name = _text(e, "zone"), _text(e, "name")
        if zone and name:
            vsys = _text(e, "vsys") or "1"
            vsys = vsys if vsys.startswith("vsys") else f"vsys{vsys}"
            z = zones.setdefault((vsys, zone), {"name": zone, "vsys": vsys, "interfaces": []})
            z["interfaces"].append(name)
    return list(zones.values())


def parse_core(raw: dict) -> dict:
    interfaces = parse_interfaces(raw.get(INTERFACES))
    vrf_of = {i["name"]: i["vrf"] for i in interfaces}
    return {"device": parse_system_info(raw.get(SYSTEM_INFO)) if raw.get(SYSTEM_INFO) else
            {"role": "firewall"},
            "interfaces": interfaces,
            "neighbors": parse_lldp(raw.get(LLDP)),
            "arp": parse_arp(raw.get(ARP), vrf_of),
            "mac_table": []}


def parse_facets(raw: dict) -> dict:
    facets = {}
    if VPN_FLOW in raw:
        facets["ipsec"] = {"tunnels": parse_vpn_flow(raw[VPN_FLOW])}
    if INTERFACES in raw:
        facets["zones"] = {"zones": parse_zones(raw[INTERFACES])}
    return facets
