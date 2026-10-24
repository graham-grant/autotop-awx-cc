"""
netparse: turns raw show-command output into the NAPALM-shaped host
record the rest of the project expects, so NAPALM itself isn't needed.

The playbook gathers each host's CLI output with ansible.builtin.raw and
writes it as:

    {"inventory_name": "r1", "platform": "ios",
     "cli": {"show version": "...", "show interfaces": "...", ...}}

normalize() fills in the fields NAPALM's getters used to provide -
"hostname", "interfaces" (get_interfaces) and "interfaces_ip"
(get_interfaces_ip) - with the same keys and value types. The "cli" dict
is kept, so feature plugins can parse their own commands out of it.

Records that already carry "interfaces"/"interfaces_ip" (e.g. JSON from
the older NAPALM-based playbook) are passed through unchanged.

Adding a platform: write a module with CORE_COMMANDS and a
parse(cli) -> {hostname, interfaces, interfaces_ip} function, and add it to
_DRIVERS / _ALIASES below.
"""
from __future__ import annotations

from netparse import ios

_DRIVERS = {"ios": ios}

# Ansible network_os / NAPALM driver names -> netparse driver
_ALIASES = {
    "ios": "ios", "iosxe": "ios", "ios-xe": "ios", "cisco.ios.ios": "ios",
}


def platform_key(network_os: str | None) -> str:
    return _ALIASES.get((network_os or "").lower(), (network_os or "").lower())


# Tried in order after login; an unknown one just errors harmlessly, and
# pager prompts are answered anyway (sshcli), so this only saves time.
_DEFAULT_PAGING = ["terminal length 0", "terminal datadump"]


def paging_commands(network_os: str) -> list[str]:
    driver = _DRIVERS.get(platform_key(network_os))
    return list(getattr(driver, "PAGING_COMMANDS", _DEFAULT_PAGING))


def core_commands(network_os: str) -> list[str]:
    driver = _DRIVERS.get(platform_key(network_os))
    return list(driver.CORE_COMMANDS) if driver else []


def normalize(record: dict) -> dict:
    record = dict(record)
    if "interfaces" in record and "interfaces_ip" in record:
        return record
    record.setdefault("hostname", record.get("inventory_name"))
    record.setdefault("interfaces", {})
    record.setdefault("interfaces_ip", {})

    cli = record.get("cli") or {}
    driver = _DRIVERS.get(platform_key(record.get("platform")))
    if driver is None:
        if cli:
            print(f"netparse: no parser for platform {record.get('platform')!r} "
                  f"({record.get('inventory_name')}) - no interface data")
        return record

    parsed = driver.parse(cli)
    record["hostname"] = parsed.get("hostname") or record["hostname"]
    record["interfaces"] = parsed.get("interfaces", {})
    record["interfaces_ip"] = parsed.get("interfaces_ip", {})
    return record
