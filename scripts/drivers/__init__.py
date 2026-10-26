"""
Platform drivers and the schema 2 device record.

Vendor knowledge lives here and nowhere else. Each driver turns what was
collected from one platform into the same vendor-neutral record, so the
base diagram and the features never read vendor output:

    {"schema": 2,
     "device":     {"name", "hostname", "role", "vendor", "platform", "model",
                    "os_version", "serial", "collected_at", "collector"},
     "interfaces": [{"name", "kind", "mac", "admin_up", "oper_up", "speed_mbps",
                     "mtu", "description", "vrf", "vlan", "parent",
                     "ipv4": ["10.0.0.1/30"], "ipv6": []}],
     "neighbors":  [{"local_intf", "protocol", "remote_name", "remote_intf", "remote_mac"}],
     "arp":        [{"ip", "mac", "intf", "vrf"}],
     "mac_table":  [{"mac", "intf", "vlan"}],
     "facets":     {"ipsec": {"tunnels": [...]}, ...},
     "raw":        {command: output},
     "audit":      {...}}            # written by collect.py, see readonly.py

The core (device .. mac_table) is what every driver fills in and what the
physical diagram reads. Facets are optional, one documented shape each,
owned by the feature that draws it. Raw output is always kept, so a parser
fix can be applied to old files with reparse() without logging in again.

A driver module declares TRANSPORT ("ssh-cli"), VENDOR, CORE_COMMANDS,
optional PAGING_COMMANDS and CONFIG_CHECK_COMMAND, FACET_COMMANDS
({facet: [commands]}), parse_core(raw) and parse_facets(raw).

Adding a platform: write the module, add it to _DRIVERS / _ALIASES below,
and give it a read-only policy in scripts/readonly.py - without one the
collector refuses to contact it.
"""
from __future__ import annotations

from drivers import ios

SCHEMA = 2

_DRIVERS = {"ios": ios}

# Ansible network_os / NAPALM driver names -> driver
_ALIASES = {
    "ios": "ios", "iosxe": "ios", "ios-xe": "ios", "cisco.ios.ios": "ios",
}

# Tried in order after login when a driver doesn't say; an unknown one just
# errors harmlessly, and pager prompts are answered anyway (sshcli).
_DEFAULT_PAGING = ["terminal length 0", "terminal datadump"]

CORE_KEYS = ("interfaces", "neighbors", "arp", "mac_table")


def platform_key(network_os: str | None) -> str:
    return _ALIASES.get((network_os or "").lower(), (network_os or "").lower())


def driver_for(network_os: str | None):
    return _DRIVERS.get(platform_key(network_os))


def platforms() -> list[str]:
    return sorted(_DRIVERS)


def paging_commands(network_os: str) -> list[str]:
    return list(getattr(driver_for(network_os), "PAGING_COMMANDS", _DEFAULT_PAGING))


def core_commands(network_os: str) -> list[str]:
    driver = driver_for(network_os)
    return list(driver.CORE_COMMANDS) if driver else []


def config_check_command(network_os: str) -> str | None:
    """A read-only command whose output changes whenever the device's
    configuration does, or None if the platform has none."""
    return getattr(driver_for(network_os), "CONFIG_CHECK_COMMAND", None)


def facet_commands(network_os: str, facets=None) -> list[str]:
    """Commands behind the given facets (all the driver supplies if None),
    in order, without duplicates."""
    table = getattr(driver_for(network_os), "FACET_COMMANDS", {})
    cmds = []
    for facet in (table if facets is None else facets):
        for cmd in table.get(facet, []):
            if cmd not in cmds:
                cmds.append(cmd)
    return cmds


def _empty(name: str, platform: str | None) -> dict:
    return {
        "schema": SCHEMA,
        "device": {"name": name, "hostname": name, "role": None, "vendor": None,
                   "platform": platform_key(platform) or None, "model": None,
                   "os_version": None, "serial": None, "collected_at": None,
                   "collector": None},
        "interfaces": [], "neighbors": [], "arp": [], "mac_table": [],
        "facets": {}, "raw": {},
    }


def build_record(name: str, platform: str, raw: dict, collected_at: str | None = None,
                 collector: str | None = None) -> dict:
    """Parse one host's raw output into a schema 2 record. A parser error
    is reported and leaves the core empty; the raw output is kept either
    way, so it can be re-parsed once the parser is fixed."""
    record = _empty(name, platform)
    record["raw"] = dict(raw)
    record["device"]["collected_at"] = collected_at
    driver = driver_for(platform)
    if driver is None:
        if raw:
            print(f"drivers: no driver for platform {platform!r} ({name}) - no interface data")
        return record
    record["device"]["collector"] = collector or driver.TRANSPORT
    record["device"]["vendor"] = driver.VENDOR
    try:
        core = driver.parse_core(raw)
        facets = driver.parse_facets(raw)
    except Exception as e:  # keep the raw output; the parser can be fixed later
        print(f"drivers: {name}: {platform_key(platform)} parser failed ({e!r}) - "
              f"raw output kept, no interface data")
        record["parse_error"] = repr(e)
        return record
    for key, value in core.pop("device", {}).items():
        if value is not None:
            record["device"][key] = value
    record["device"]["hostname"] = record["device"]["hostname"] or name
    for key in CORE_KEYS:
        record[key] = core.get(key, [])
    record["facets"] = facets
    return record


def reparse(record: dict) -> dict:
    """Re-run the current parser over a record's raw output, keeping its
    identity, collection details and audit."""
    record = load(record)
    dev = record["device"]
    fresh = build_record(dev["name"], dev["platform"], record.get("raw") or {},
                         dev.get("collected_at"), dev.get("collector"))
    for key in ("audit",):
        if key in record:
            fresh[key] = record[key]
    return fresh


def load(record: dict) -> dict:
    """Any facts record -> schema 2. Schema 2 passes through; schema 1
    (raw "cli" output, or the older NAPALM-shaped JSON) is upgraded."""
    if record.get("schema") == SCHEMA:
        return record
    if record.get("schema") not in (None, 1):
        raise ValueError(f"facts schema {record.get('schema')!r} is newer than this "
                         f"code understands ({SCHEMA})")
    name = record.get("inventory_name") or record.get("hostname")
    platform = record.get("platform")
    if "interfaces" in record and "interfaces_ip" in record:
        upgraded = _from_napalm(record, name, platform)
    else:
        upgraded = build_record(name, platform, record.get("cli") or {})
    if isinstance(record.get("ipsec"), dict):  # pre-normalised, from another collector
        upgraded["facets"]["ipsec"] = record["ipsec"]
    if "audit" in record:
        upgraded["audit"] = record["audit"]
    return upgraded


def _from_napalm(record: dict, name: str, platform: str | None) -> dict:
    """NAPALM get_interfaces / get_interfaces_ip shape -> schema 2 core."""
    out = _empty(name, platform)
    out["device"]["hostname"] = record.get("hostname") or name
    out["device"]["collector"] = "napalm"
    out["raw"] = dict(record.get("cli") or {})
    ips = record.get("interfaces_ip") or {}
    names = list(record.get("interfaces") or {}) + [n for n in ips if n not in (record.get("interfaces") or {})]
    for intf_name in names:
        intf = (record.get("interfaces") or {}).get(intf_name, {})
        fam = ips.get(intf_name, {})
        speed = intf.get("speed")
        out["interfaces"].append({
            "name": intf_name, "kind": ios._kind_and_parent(intf_name)[0],
            "mac": (intf.get("mac_address") or "").lower() or None,
            "admin_up": intf.get("is_enabled"), "oper_up": intf.get("is_up"),
            "speed_mbps": speed, "mtu": intf.get("mtu"),
            "description": intf.get("description", ""), "vrf": "default", "vlan": None,
            "parent": ios._kind_and_parent(intf_name)[1],
            "ipv4": [f"{a}/{v['prefix_length']}" for a, v in (fam.get("ipv4") or {}).items()],
            "ipv6": [f"{a}/{v['prefix_length']}" for a, v in (fam.get("ipv6") or {}).items()],
        })
    return out


# -- small accessors used across the project ---------------------------------

def name(record: dict) -> str:
    return record["device"]["name"]


def interface(record: dict, intf_name: str) -> dict | None:
    for intf in record.get("interfaces", []):
        if intf["name"] == intf_name:
            return intf
    return None
