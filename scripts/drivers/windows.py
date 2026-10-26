"""
Windows Server driver: the JSON printed by the fixed PowerShell scripts in
drivers/powershell/ -> the schema 2 core, plus the server facet.

Collected over OpenSSH with PowerShell (sshcli.PowerShellSession). A
command is "powershell:<script>.ps1", checked by readonly.PowerShellPolicy,
and raw output is the JSON each script prints.

Windows PowerShell 5.1's ConvertTo-Json writes enums as numbers, PowerShell
7 as names, so both are accepted. Windows doesn't run LLDP by default, so
`neighbors` is empty: servers are placed on the diagram by their NIC MAC
addresses in the switches' MAC tables (links.py). An LBFO team is an
interface of kind "lag", and its member NICs have it as their parent, so a
team's MAC found on a switch port draws one link to the team.

Loopback (127.0.0.0/8, ::1) and link-local IPv4 (169.254.0.0/16)
addresses are left out: every server has them, and they would otherwise
look like one shared subnet.
"""
from __future__ import annotations

import ipaddress
import json

from drivers.common import norm_mac

TRANSPORT = "powershell-ssh"
VENDOR = "microsoft"
PAGING_COMMANDS: list[str] = []

CORE_SCRIPT = "powershell:windows_core.ps1"
ROLES_SCRIPT = "powershell:windows_roles.ps1"
CORE_COMMANDS = [CORE_SCRIPT]
FACET_COMMANDS = {"server": [ROLES_SCRIPT]}
# No config-change marker: the policy and the fixed scripts are the guard.

_FAMILY = {2: "IPv4", 23: "IPv6", "IPv4": "IPv4", "IPv6": "IPv6"}
_ADMIN_UP = {1: True, 2: False, "Up": True, "Down": False}
_NEIGHBOR_GONE = {0, 1, "Unreachable", "Incomplete"}


def _json(text: str | None):
    if not text:
        return None
    return json.loads(text)


def _list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _keep(cidr: str) -> bool:
    ip = ipaddress.ip_interface(cidr).ip
    return not (ip.is_loopback or (ip.version == 4 and ip.is_link_local))


def parse_core_json(data: dict) -> dict:
    data = data or {}
    computer = data.get("computer") or {}
    os_info = data.get("os") or {}
    teams = {t.get("Name"): _list(t.get("Members")) for t in _list(data.get("teams"))
             if t.get("Name")}
    member_of = {m: team for team, members in teams.items() for m in members}

    addresses: dict[str, dict] = {}
    for a in _list(data.get("addresses")):
        alias, ip, prefix = a.get("InterfaceAlias"), a.get("IPAddress"), a.get("PrefixLength")
        family = _FAMILY.get(a.get("AddressFamily"))
        if not alias or not ip or prefix is None or not family:
            continue
        cidr = f"{ip.split('%')[0]}/{prefix}"
        if _keep(cidr):
            addresses.setdefault(alias, {"IPv4": [], "IPv6": []})[family].append(cidr)

    interfaces = []
    for n in _list(data.get("adapters")):
        name = n.get("Name")
        if not name:
            continue
        speed = n.get("ReceiveLinkSpeed")
        vlan = n.get("VlanID")
        addr = addresses.pop(name, {"IPv4": [], "IPv6": []})
        interfaces.append({
            "name": name, "kind": "lag" if name in teams else "physical",
            "mac": norm_mac(n.get("MacAddress")),
            "admin_up": _ADMIN_UP.get(n.get("AdminStatus")),
            "oper_up": None if n.get("Status") is None else n.get("Status") == "Up",
            "speed_mbps": int(speed) // 1_000_000 if isinstance(speed, (int, float)) and speed
            else None,
            "mtu": n.get("MtuSize") or None, "description": n.get("InterfaceDescription") or "",
            "vrf": "default",
            "vlan": int(vlan) if str(vlan or "").isdigit() and int(vlan) else None,
            "parent": member_of.get(name), "ipv4": addr["IPv4"], "ipv6": addr["IPv6"],
        })
    for alias, addr in addresses.items():  # addresses on interfaces Get-NetAdapter hides
        if addr["IPv4"] or addr["IPv6"]:
            interfaces.append({
                "name": alias, "kind": "virtual", "mac": None, "admin_up": None,
                "oper_up": None, "speed_mbps": None, "mtu": None, "description": "",
                "vrf": "default", "vlan": None, "parent": None,
                "ipv4": addr["IPv4"], "ipv6": addr["IPv6"]})

    arp = []
    for nb in _list(data.get("neighbors")):
        mac = norm_mac(nb.get("LinkLayerAddress"))
        if not mac or nb.get("State") in _NEIGHBOR_GONE or mac == "00:00:00:00:00:00" \
                or int(mac[:2], 16) & 1:  # broadcast and multicast
            continue
        arp.append({"ip": nb.get("IPAddress"), "mac": mac, "intf": nb.get("InterfaceAlias"),
                    "vrf": "default"})

    model = " ".join(p for p in (computer.get("Manufacturer"), computer.get("Model")) if p)
    version = " ".join(p for p in (os_info.get("Caption"), os_info.get("Version")) if p)
    hostname = computer.get("DNSHostName") or computer.get("Name")
    return {"device": {"hostname": hostname.lower() if hostname else None,
                       "model": model or None, "os_version": version or None,
                       "serial": (data.get("serial") or "").strip() or None, "role": "server"},
            "interfaces": interfaces, "neighbors": [], "arp": arp, "mac_table": []}


def parse_core(raw: dict) -> dict:
    return parse_core_json(_json(raw.get(CORE_SCRIPT)))


def parse_facets(raw: dict) -> dict:
    if ROLES_SCRIPT not in raw:
        return {}
    core = _json(raw.get(CORE_SCRIPT)) or {}
    computer = core.get("computer") or {}
    roles = [{"name": r.get("Name"), "display_name": r.get("DisplayName")}
             for r in _list(_json(raw[ROLES_SCRIPT])) if r.get("Name")]
    domain = computer.get("Domain") if computer.get("PartOfDomain") else None
    return {"server": {"domain": domain, "roles": roles}}
