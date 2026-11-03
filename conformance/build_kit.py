#!/usr/bin/env python3
"""
Builds a conformance kit from the reference implementation (this
repository): runs every vector against the code here and records the
exact result as the vector's expectation. See conformance/run_kit.py for
what a kit is and how it is run.

A stage's kit describes the code as it is at the end of that stage of the
production rebuild (see docs/design-archive/2026-10-31/
production-rebuild-roadmap.md), which can be a subset of this repository:
stage 1 has only the Cisco IOS driver and read-only policy. The kit is
built with the platforms a stage doesn't have yet removed from
readonly._POLICIES and drivers._DRIVERS (stage_view()), so its
expectations are what the stage's code must do, including refusing those
platforms.

Usage: python3 conformance/build_kit.py <stage> [<out.json>]
       (default out: conformance/stage<stage>/kit.json)

tests/test_conformance.py fails if a committed kit no longer matches what
this builds, so a behaviour change can't go unnoticed: rebuild the kit,
and give the new one to the production rebuild with the change.
"""
from __future__ import annotations

import contextlib
import copy
import json
import sys
from pathlib import Path

import run_kit  # sets up sys.path for scripts/ and conformance/

import drivers
import readonly
from drivers import ios

ROOT = run_kit.ROOT
FIXTURES = ROOT / "tests" / "fixtures"

STAGES = {
    1: {"platforms": ["ios"], "vendors": ["cisco"], "collectors": ["ssh-cli"],
        # the reference's classes that come in a later stage
        "later_classes": ["PowerShellSession"],
        "files": ["scripts/config.py", "scripts/readonly.py", "scripts/sshcli.py",
                  "scripts/commands.py", "scripts/collect.py", "scripts/audit.py",
                  "scripts/drivers/__init__.py", "scripts/drivers/common.py",
                  "scripts/drivers/ios.py", "conformance/run_kit.py",
                  "conformance/check_facts.py", "tests/fake_ssh.py"]},
}

# Standard library modules the project may import. Anything else that
# isn't one of the project's own modules is a dependency, which the rules
# forbid.
STDLIB = ["__future__", "argparse", "ast", "base64", "collections", "contextlib", "copy",
          "dataclasses", "datetime", "errno", "hashlib", "heapq", "html", "importlib", "io",
          "ipaddress", "json", "math", "os", "pathlib", "pkgutil", "posixpath", "pty", "random",
          "re", "select", "shlex", "shutil", "signal", "statistics", "struct", "sys",
          "sysconfig", "tempfile", "termios", "threading", "time", "traceback", "tty",
          "unittest", "urllib", "xml", "zipfile", "zlib"]


def project_modules():
    names = {p.stem for p in (ROOT / "scripts").glob("*.py")}
    names |= {p.name for p in (ROOT / "scripts").iterdir()
              if p.is_dir() and (p / "__init__.py").exists()}
    names |= {p.stem for p in (ROOT / "conformance").glob("*.py")}
    return sorted(names | {"_hanan", "support"})

# -- facts schema ---------------------------------------------------------------


def leaf(*types, **kw):
    return {"type": list(types), **kw}


def obj(keys, optional=()):
    return {"type": ["dict"], "keys": keys, "optional": list(optional)}


def lst(items):
    return {"type": ["list"], "items": items}


def facts_schema(stage):
    s = STAGES[stage]
    device = obj({
        "name": leaf("str"), "hostname": leaf("str"),
        "role": leaf("str", "null", enum=["router", "switch", "firewall", "server"]),
        "vendor": leaf("str", "null", enum=s["vendors"]),
        "platform": leaf("str", "null", enum=s["platforms"]),
        "model": leaf("str", "null"), "os_version": leaf("str", "null"),
        "serial": leaf("str", "null"),
        "collected_at": leaf("str", "null", format="iso_time"),
        "collector": leaf("str", "null", enum=s["collectors"]),
    })
    interface = obj({
        "name": leaf("str"),
        "kind": leaf("str", enum=["physical", "subinterface", "lag", "vlan", "loopback",
                                  "tunnel", "virtual"]),
        "mac": leaf("str", "null", format="mac"),
        "admin_up": leaf("bool", "null"), "oper_up": leaf("bool", "null"),
        "speed_mbps": leaf("int", "float", "null"), "mtu": leaf("int", "null"),
        "description": leaf("str"), "vrf": leaf("str"), "vlan": leaf("int", "null"),
        "parent": leaf("str", "null"),
        "ipv4": lst(leaf("str", format="ipv4_cidr")),
        "ipv6": lst(leaf("str", format="ipv6_cidr")),
    })
    neighbor = obj({
        "local_intf": leaf("str"), "protocol": leaf("str", enum=["lldp", "cdp"]),
        "remote_name": leaf("str", "null"), "remote_intf": leaf("str", "null"),
        "remote_mac": leaf("str", "null", format="mac"), "remote_ip": leaf("str", "null"),
        "remote_platform": leaf("str", "null"),
        "remote_capabilities": lst(leaf("str", enum=["router", "bridge", "phone", "wlan",
                                                     "station", "docsis", "other",
                                                     "repeater"])),
    })
    arp = obj({"ip": leaf("str", format="ipv4"), "mac": leaf("str", "null", format="mac"),
               "intf": leaf("str", "null"), "vrf": leaf("str")})
    mac = obj({"mac": leaf("str", format="mac"), "intf": leaf("str"),
               "vlan": leaf("int", "null")})
    facets = obj({
        "ipsec": obj({"tunnels": lst(obj({
            "interface": leaf("str"), "local_address": leaf("str", "null"),
            "peer_address": leaf("str"),
            "status": leaf("str", enum=["up", "down", "unknown"]),
            "vendor_status": leaf("str", "null")}))}),
        "vlans": obj({
            "vlans": lst(obj({"id": leaf("int"), "name": leaf("str"),
                              "ports": lst(leaf("str"))})),
            "trunks": lst(obj({"port": leaf("str"), "native": leaf("int", "null"),
                               "allowed": leaf("str", format="vlan_list"),
                               "active": leaf("str", format="vlan_list")}))}),
    }, optional=["ipsec", "vlans"])
    audit = obj({
        "policy": leaf("str", enum=s["platforms"]),
        "commands": lst(obj({
            "time": leaf("str", format="iso_time"), "command": leaf("str"),
            "kind": leaf("str", enum=["session", "check", "collect"]),
            "status": leaf("str", enum=["sent", "ok", "refused", "timeout", "cancelled",
                                        "error"])})),
        "config_check": obj({
            "status": leaf("str", enum=["unchanged", "changed", "unavailable", "unsupported"]),
            "command": leaf("str"), "before": leaf("str", "null"),
            "after": leaf("str", "null")}, optional=["command", "before", "after"]),
    })
    return obj({
        "schema": leaf("int", enum=[drivers.SCHEMA]), "device": device,
        "interfaces": lst(interface), "neighbors": lst(neighbor), "arp": lst(arp),
        "mac_table": lst(mac), "facets": facets, "raw": {"type": ["dict"], "values": leaf("str")},
        "parse_error": leaf("str"), "audit": audit,
    }, optional=["parse_error", "audit"])


# -- the stage's view of the reference code -----------------------------------------

@contextlib.contextmanager
def stage_view(stage):
    """The reference code with only the stage's platforms: the others have
    no read-only policy and no driver, exactly as in the stage's code."""
    keep = STAGES[stage]["platforms"]
    policies, drivers_ = readonly._POLICIES, drivers._DRIVERS
    readonly._POLICIES = {k: v for k, v in policies.items() if k in keep}
    drivers._DRIVERS = {k: v for k, v in drivers_.items() if k in keep}
    run_kit.EXCLUDE_CLASSES = set(STAGES[stage]["later_classes"])
    try:
        yield
    finally:
        readonly._POLICIES, drivers._DRIVERS = policies, drivers_
        run_kit.EXCLUDE_CLASSES = set()


# -- inputs ------------------------------------------------------------------------

def _fixture(path):
    data = json.loads((FIXTURES / path).read_text(encoding="utf-8"))
    return data["inventory_name"], data["platform"], data["cli"]


FIXTURE_FILES = ["facts/hq.json", "facts/branch1.json", "facts/core1.json",
                 "l2/rtr1.json", "l2/sw1.json", "l2/sw2.json"]

IOS_ALLOWED = [
    "show version", "show interfaces", "show ip interface", "show crypto session detail",
    "show running-config | include configuration change", "show ip route vrf MGMT",
    "show interfaces GigabitEthernet1/0/1", "show mac address-table | exclude CPU",
    "show lldp neighbors detail | section Local Intf", "show logging | count CONFIG_I",
    "terminal length 0", "terminal width 512", "terminal datadump",
    # more shapes the policy allows, recorded so the rebuild allows exactly these
    "terminal width 0", "terminal width 999", "show running-config", "show startup-config",
    "show tech-support", "show flash:", "show reload", "show archive",
    "show version | exclude x | begin y", "show running-config | section interface",
    "show version | include a;reload", "show version | include \\x",
]

IOS_REFUSED = [
    "configure terminal", "conf t", "config", "write memory", "wr", "write erase",
    "copy running-config startup-config", "copy tftp: flash:", "reload", "reload in 5",
    "delete flash:x.bin", "erase startup-config", "format flash:", "clear counters",
    "clear ip bgp *", "debug all", "undebug all", "no ip routing", "default interface Gi1",
    "crypto key zeroize", "license smart register", "install add file x", "request platform",
    "test crypto", "send log hello", "archive download-sw x", "event manager run x",
    "tclsh", "boot system flash:x", "monitor capture", "ping 10.0.0.1", "ssh -l a b",
    "telnet 10.0.0.1",
    "show running-config | redirect flash:x.txt", "show running-config | tee flash:x",
    "show running-config | append flash:x", "show version | include a | redirect flash:y",
    "show version\nconfigure terminal", "show version\rreload", "show version?",
    "show run\t", "sh run", "show", "", "  show version", "show  version",
    "show version ", "terminal length 24 ; reload", "terminal monitor",
    "terminal length 0\nreload",
    "set deviceconfig system hostname x", "commit", "request restart system",
    "Set-NetAdapter -Name x", "Restart-Computer", "Remove-Item C:\\x",
    # pipes and filters
    "show version | redirect bootflash:x", "show version | include x | tee y",
    "show version | i x", "show version | inc x", "show version |include x",
    "show version| include x", "show version | include", "show version | include x|redirect y",
    "show version | include |", "show version | format x", "show version | xml",
    "show version | json", "show version | count", "show version | begin",
    "show version | include x?", "show version | include x\ty",
    # case, look-alike and invisible characters, control characters
    "Show version", "SHOW VERSION", "show\u00a0version", "show version\u200b",
    "\uff53\uff48\uff4f\uff57 version", "show version\x00", "show version\x1b[A",
    "show version\x7f", "\x03", "show version;reload", "show version && reload",
    "show version ! reload", "show version\r\n",
    # terminal settings outside the three allowed
    "terminal length 0 ", "terminal length 1", "terminal width 1000", "terminal width 01",
    "terminal no monitor", "term len 0", "terminal datadump x",
    "terminal length 0 | include x", "terminal", "terminal exec prompt timestamp",
    # session words: sent by the session itself, never as a command
    "enable", "exit", "end", "disable", "logout", "quit",
    # abbreviations of show
    "sh", "s", "sho version", "n", None, 42,
]


def _deny_variants():
    out = []
    for verb in readonly.DENY_VERBS:
        if verb.endswith("-"):
            out += [verb + "Item x", verb.upper() + "ITEM", verb.capitalize() + "Item"]
        elif verb == "out-file":
            out += ["Out-File x"]
        else:
            out += [verb, verb + " all", verb.upper()]
            if len(verb) >= 3:
                out += [verb[:2], verb[:3] + " x"]
    seen, unique = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


PLATFORM_INPUTS = ["ios", "IOS", "iosxe", "ios-xe", "IOS-XE", "cisco.ios.ios", "panos",
                   "pan-os", "paloaltonetworks.panos", "windows", "win", "nxos", "eos",
                   "junos", "", None]

MAC_INPUTS = ["5254.0012.3456", "52-54-00-12-34-56", "52:54:00:12:34:56", "5254.0012.345",
              "5254.0012.34567", "zz54.0012.3456", "52:54:00:12:34:5G", "", None,
              "52 54 00 12 34 56", "00A1.B2C3.0101", "52:54:00:12:34:56:78"]

INTF_KNOWN = ["GigabitEthernet1/0/1", "GigabitEthernet1/0/2", "Port-channel1",
              "TenGigabitEthernet1/1/1", "Vlan10", "Loopback0", "Tunnel1"]
INTF_INPUTS = ["GigabitEthernet1/0/1", "gigabitethernet1/0/1", "Gi1/0/1", "Gi 1/0/1",
               "G1/0/1", "Po1", "Te1/1/1", "Vl10", "Lo0", "Tu1", "Fa0/1", "Gi1/0/9", "", None,
               "eth0", "T1/1/1"]

ERROR_OUTPUTS = ["% Invalid input detected at '^' marker.\n",
                 "                 ^\n% Invalid input detected at '^' marker.\n",
                 "% Unrecognized command\n", "% Unknown command\n", "% Incomplete command.\n",
                 "% Ambiguous command:  \"show x\"\n", "Unrecognized command found\n",
                 "line1\nline2\n% Invalid input\n", "Cisco IOS XE Software\n", "", "\n\n",
                 "  % Invalid input\n", "^\n"]

MARKER_OUTPUTS = ["! Last configuration change at 09:10:00 UTC Mon Oct 26 2026 by admin\n",
                  "  ! Last configuration change at x  \n\n", "", "\n"]

SNIPPETS = {
    "parse_show_ip_interface": [
        "Vlan10 is up, line protocol is up\n  Internet address is 10.1.1.1/24\n"
        "  Secondary address 10.2.2.1/24\n  VPN Routing/Forwarding \"MGMT\"\n", ""],
    "parse_show_interfaces": [
        "GigabitEthernet1.20 is up, line protocol is up\n"
        "  Encapsulation 802.1Q Virtual LAN, Vlan ID  20.\n"
        "Vlan30 is up, line protocol is up\nPort-channel1 is up, line protocol is up\n"
        "Loopback0 is up, line protocol is up\n"
        "Tunnel5 is administratively down, line protocol is down\n"
        "  MTU 9976 bytes, BW 100 Kbit/sec, DLY 50000 usec,\n"
        "GigabitEthernet0/1 is down, line protocol is down (notconnect)\n"
        "  BW 1500 Kbit/sec\n", ""],
    "parse_arp": ["", "Protocol  Address  Age (min)  Hardware Addr   Type   Interface\n"
                      "Internet  10.0.0.1        -   5254.0000.0001  ARPA   GigabitEthernet0/0\n"
                      "Internet  10.0.0.2       12   5254.0000.0002  ARPA\n"],
    "parse_mac_table": ["", "  10    00a1.b2c3.0001    DYNAMIC     Gi1/0/1\n"
                            " All    0100.0ccc.cccc    STATIC      CPU\n"
                            "  20    00a1.b2c3.0002    DYNAMIC     Po1\n"],
    "parse_crypto_session": ["", "Interface: Tunnel9\nSession status: DOWN-NEGOTIATING\n"
                                 "Peer: 192.0.2.9 port 500\n"
                                 "  IPSEC FLOW: permit 47 host 192.0.2.1 host 192.0.2.9\n"
                                 "Interface: Tunnel8\nSession status: UP-NO-IKE\n"
                                 "Interface: Tunnel7\nSession status: WEIRD\n"
                                 "Peer: 192.0.2.7 port 500\n"],
    "parse_vlan_brief": ["", "VLAN Name                             Status    Ports\n"
                             "---- -------------------------------- --------- ----------\n"
                             "1    default                          active    Gi1/0/3, Gi1/0/4\n"
                             "                                                Gi1/0/5\n"
                             "10   users                            active    \n"
                             "1002 fddi-default                     act/unsup \n"],
    "parse_interfaces_trunk": ["", "Port        Mode         Encapsulation  Status        Native vlan\n"
                                   "Gi1/0/1     on           802.1q         trunking      1\n\n"
                                   "Port        Vlans allowed on trunk\nGi1/0/1     none\n\n"
                                   "Port        Vlans allowed and active in management domain\n"
                                   "Gi1/0/1     1,10,20\n"],
    "parse_lldp_neighbors": [""],
    "parse_cdp_neighbors": [""],
    "parse_show_version": ["", "junk\n"],
}

# which raw command each parser reads
PARSER_INPUT = {
    "parse_show_version": "show version", "parse_show_interfaces": "show interfaces",
    "parse_show_ip_interface": "show ip interface",
    "parse_lldp_neighbors": "show lldp neighbors detail",
    "parse_cdp_neighbors": "show cdp neighbors detail", "parse_arp": "show ip arp",
    "parse_mac_table": "show mac address-table dynamic",
    "parse_vlan_brief": "show vlan brief", "parse_interfaces_trunk": "show interfaces trunk",
    "parse_crypto_session": "show crypto session detail",
}

COLLECTED_AT = "2026-10-26T01:00:00+00:00"


def _creds(**kw):
    return {"NET_USERNAME": "admin", "NET_PASSWORD": "secret", **kw}


# -- vectors -----------------------------------------------------------------------

def vector_specs(stage):
    """(section, id, vector) for every vector, without expectations."""
    specs = []

    def add(section, vid, **v):
        specs.append((section, vid, v))

    # constants that pin the safety-critical behaviour and the parsers
    consts = {
        "readonly": ["DENY_VERBS", "IOS_PIPE_FILTERS", "_IOS_SHOW", "_IOS_FILTER",
                     "_IOS_SESSION", "_BAD_CHARS"],
        "sshcli": ["_ANSI", "_MORE", "_MORE_WAITING", "_USER_PROMPT", "_PASS_PROMPT",
                   "_HOSTKEY_PROMPT", "_CLI_PROMPT", "_CONFIRM_PROMPT", "_CTRL_C",
                   "_AUTH_FAILED"],
        "drivers": ["SCHEMA", "_ALIASES", "_DEFAULT_PAGING", "CORE_KEYS"],
        "drivers.common": ["_HEX", "_NAME"],
        "drivers.ios": ["TRANSPORT", "VENDOR", "CORE_COMMANDS", "PAGING_COMMANDS",
                        "CONFIG_CHECK_COMMAND", "FACET_COMMANDS", "_INTF_HEADER", "_MAC",
                        "_MTU", "_BW", "_DOT1Q", "_IP_PRIMARY", "_IP_SECONDARY", "_VRF",
                        "_HOSTNAME", "_VERSION", "_MODEL", "_SERIAL", "_MEMBERS",
                        "_DOTTED_MAC", "_ARP_LINE", "_MAC_LINE", "_VLAN_LINE", "_CDP_CAPS",
                        "_LLDP_CAPS", "_SA_RE", "_FLOW_RE", "_SWITCH_MODEL", "_KINDS"],
        "collect": ["USERNAME_VARS", "PASSWORD_VARS", "ENABLE_VARS"],
        "audit": ["SESSION_WORDS"],
        "config": ["ENABLED_FEATURES"],
    }
    for module, names in consts.items():
        for name in names:
            add("constants", f"{module}.{name}", kind="attr", module=module, name=name)

    # what can reach a device, and what is imported
    allowed = STDLIB + project_modules()
    for f in STAGES[stage]["files"]:
        if f != "tests/fake_ssh.py":
            add("structure", f"calls {f}", kind="calls", file=f)
        add("structure", f"imports {f}", kind="imports", file=f, allowed=allowed)

    # platforms and the driver tables
    add("platforms", "drivers.platforms", kind="call", module="drivers", func="platforms")
    add("platforms", "readonly.platforms", kind="call", module="readonly", func="platforms")
    for p in PLATFORM_INPUTS:
        add("platforms", f"platform_key({p!r})", kind="call", module="drivers",
            func="platform_key", args=[p])
        add("platforms", f"driver_for({p!r})", kind="call", module="drivers",
            func="driver_for", args=[p], then="__name__")
        add("platforms", f"policy_for({p!r})", kind="call", module="readonly",
            func="policy_for", args=[p], then="name")
        for func in ("transport", "core_commands", "paging_commands", "config_check_command",
                     "facet_commands"):
            add("platforms", f"{func}({p!r})", kind="call", module="drivers", func=func,
                args=[p if p is not None else ""])
    for facets in (["ipsec"], ["vlans", "ipsec"], ["zones", "server"], [], ["vlans", "vlans"]):
        add("platforms", f"facet_commands('ios', {facets})", kind="call", module="drivers",
            func="facet_commands", args=["ios", facets])

    # the read-only policy
    for i, cmd in enumerate(IOS_ALLOWED + IOS_REFUSED + _deny_variants()):
        add("policy", f"ios#{i} {cmd!r}"[:90], kind="policy", platform="ios", command=cmd)
    for p in ("panos", "windows", "nxos", "junos", "iosxe", "", None):
        add("policy", f"{p!r}: show version", kind="policy", platform=p, command="show version")

    # what collection will send
    for p in ("ios", "iosxe", "IOS", "panos", "windows", "junos", ""):
        add("plan", f"commands({p!r})", kind="call", module="commands", func="commands", args=[p])
    for p in ("ios", "iosxe", "panos", "windows", "junos", "", "nxos"):
        add("plan", f"plan({p!r})", kind="call", module="collect", func="plan", args=[p])
    for p in ("ios", "junos", "panos"):
        add("plan", f"print_plan({p!r})", kind="stdout", module="collect", func="print_plan",
            args=[p])

    # helpers
    for m in MAC_INPUTS:
        add("helpers", f"norm_mac({m!r})", kind="call", module="drivers.common",
            func="norm_mac", args=[m])
    for n in INTF_INPUTS:
        add("helpers", f"canonical_intf({n!r})", kind="call", module="drivers.common",
            func="canonical_intf", args=[n, INTF_KNOWN])
    add("helpers", "canonical_intf ambiguous", kind="call", module="drivers.common",
        func="canonical_intf", args=["Gi1", ["GigabitEthernet1", "Gig1"]])
    add("helpers", "_mac", kind="call", module="drivers.ios", func="_mac", args=["5254.00AB.cdef"])
    for n in ("GigabitEthernet1.20", "Loopback0", "Tunnel1", "Port-channel1", "Vlan10", "BDI5",
              "NVI0", "Null0", "Virtual-Access1", "GigabitEthernet1", "Port-channel1.5"):
        add("helpers", f"_kind_and_parent({n!r})", kind="call", module="drivers.ios",
            func="_kind_and_parent", args=[n])
    for i, o in enumerate(ERROR_OUTPUTS):
        add("helpers", f"_looks_like_error#{i}", kind="call", module="collect",
            func="_looks_like_error", args=[o])
    for i, o in enumerate(MARKER_OUTPUTS):
        add("helpers", f"config_marker#{i}", kind="call", module="drivers",
            func="config_marker", args=["ios", o])
    add("helpers", "config_marker junos", kind="call", module="drivers", func="config_marker",
        args=["junos", "  x  \n"])

    # parsers, on every fixture's output and on edge cases
    fixtures = [_fixture(f) for f in FIXTURE_FILES]
    version = (FIXTURES / "cli" / "c8000v_show_version.txt").read_text(encoding="utf-8")
    for func, command in PARSER_INPUT.items():
        inputs = [(name, raw[command]) for name, _, raw in fixtures if command in raw]
        if func == "parse_show_version":
            inputs.append(("c8000v", version))
        inputs += [(f"snippet{i}", s) for i, s in enumerate(SNIPPETS.get(func, []))]
        for label, text in inputs:
            add("parsers", f"{func}[{label}]", kind="call", module="drivers.ios", func=func,
                args=[text])

    # whole records
    for name, platform, raw in fixtures:
        add("records", f"build_record[{name}]", kind="call", module="drivers",
            func="build_record", args=[name, platform, raw, COLLECTED_AT])
    add("records", "build_record[empty]", kind="call", module="drivers", func="build_record",
        args=["x", "ios", {}])
    add("records", "build_record[collector]", kind="call", module="drivers",
        func="build_record", args=["x", "IOS-XE", {"show version": version}, None, "test"])
    for p in ("junos", "panos", None):
        add("records", f"build_record[{p!r}]", kind="call", module="drivers",
            func="build_record", args=["j", p, {"show version": "x"}])
    hq = drivers.build_record(*fixtures[0][:3])
    add("records", "name", kind="call", module="drivers", func="name", args=[hq])
    for n in ("Tunnel1", "GigabitEthernet0/0", "Gi0/0", "nope"):
        add("records", f"interface({n!r})", kind="call", module="drivers", func="interface",
            args=[hq, n])

    # the audit
    def audit_record(platform="ios", commands=None, check=None, **extra):
        rec = {"inventory_name": "r1", "platform": platform, "cli": {},
               "audit": {"policy": platform, "commands": commands if commands is not None else [
                   {"command": "terminal length 0", "kind": "session", "status": "ok"},
                   {"command": "show version", "kind": "collect", "status": "ok"},
                   {"command": "exit", "kind": "session", "status": "sent"}],
                   "config_check": check or {"status": "unchanged"}}}
        rec.update(extra)
        return rec
    ok = [{"command": "terminal length 0", "kind": "session", "status": "ok"},
          {"command": "show version", "kind": "collect", "status": "ok"}]
    cases = {
        "clean": audit_record(),
        "write memory": audit_record(commands=ok + [
            {"command": "write memory", "kind": "collect", "status": "ok"}]),
        "refused": audit_record(commands=ok + [
            {"command": "configure terminal", "kind": "collect", "status": "refused"}]),
        "cancelled": audit_record(commands=ok + [
            {"command": "show x", "kind": "collect", "status": "cancelled"}]),
        "enable": audit_record(commands=[
            {"command": "enable", "kind": "session", "status": "sent"}] + ok),
        "enable as collect": audit_record(commands=[
            {"command": "enable", "kind": "collect", "status": "ok"}] + ok),
        "keygen late": audit_record(commands=ok + [
            {"command": "type=keygen", "kind": "session", "status": "ok"}]),
        "changed": audit_record(check={"status": "changed", "before": "a", "after": "b"}),
        "unavailable": audit_record(check={"status": "unavailable",
                                           "command": "show running-config | include x"}),
        "unsupported": audit_record(check={"status": "unsupported"}),
        "no check": audit_record(check={"status": None}),
        "panos": audit_record(platform="panos"),
        "junos": audit_record(platform="junos"),
        "schema 2": {"device": {"name": "r2", "platform": "ios"},
                     "audit": {"commands": ok, "config_check": {"status": "unchanged"}}},
        "no audit": {"inventory_name": "r1", "platform": "ios", "cli": {}},
    }
    for label, rec in cases.items():
        add("audit", f"verify[{label}]", kind="call", module="audit", func="verify", args=[rec])
    add("audit", "_platform", kind="call", module="audit", func="_platform",
        args=[cases["schema 2"]])
    add("audit", "_name", kind="call", module="audit", func="_name", args=[{}, "dflt"])

    # live sessions against the fake device
    sessions = {
        "plain show version": dict(mode="plain", steps=[["run", "show version", None]]),
        "in-band login and pager": dict(mode="inband", steps=[
            ["run", "show interfaces status", None], ["run", "show version", None]]),
        "pager erase arrives alone": dict(mode="inband", env={"FAKE_SSH_SLOW_ERASE": "1"},
                                          steps=[["run", "show interfaces status", None],
                                                 ["run", "show version", None]]),
        "paging disabled": dict(mode="inband", steps=[
            ["disable_paging", ["terminal length 0", "terminal datadump"]],
            ["run", "show interfaces status", None]]),
        "wrong password": dict(mode="plain", password="nope"),
        "in-band login rejected": dict(mode="wrongpass"),
        "refused command never sent": dict(mode="plain", steps=[
            ["run", "configure terminal", None], ["run", "show version", None]]),
        "second line never sent": dict(mode="plain", steps=[
            ["run", "show version\nreload", None], ["run", "show version", None]]),
        "no policy sends nothing": dict(mode="plain", policy=None,
                                        steps=[["run", "show version", None]]),
        "confirmation cancelled": dict(mode="plain", steps=[
            ["run", "show confirm-test", None], ["run", "show version", None]]),
        "timeout cancelled": dict(mode="plain", steps=[
            ["run", "show hang", 1], ["run", "show version", None]]),
        "refused paging command": dict(mode="plain", steps=[
            ["disable_paging", ["terminal length 0", "reload"]], ["run", "show version", None]]),
        "unknown command": dict(mode="plain", steps=[["run", "show bogus", None]]),
    }
    for label, s in sessions.items():
        s.setdefault("policy", "ios")
        add("session", label, kind="session", **s)

    collects = {
        "audit matches device": dict(mode="plain", env=_creds(),
                                     commands=["show version", "show interfaces status"]),
        "config change": dict(mode="configchange", env=_creds(), commands=["show version"]),
        "refused before connecting": dict(mode="plain", env=_creds(),
                                          commands=["show version", "clear counters"]),
        "AWX Network credential": dict(mode="inband", env={"ANSIBLE_NET_USERNAME": "admin",
                                                           "ANSIBLE_NET_PASSWORD": "secret"},
                                       commands=["show version", "show bogus",
                                                 "show interfaces status"]),
        "no credentials": dict(mode="plain", env={}, commands=["show version"]),
        "first credential wins": dict(mode="plain",
                                      env=_creds(ANSIBLE_NET_USERNAME="other",
                                                 ANSIBLE_NET_PASSWORD="other"),
                                      commands=["show version"]),
    }
    for label, c in collects.items():
        add("collect", label, kind="collect", name="AT-8K-1", host="sw", platform="ios", **c)
    for p in ("junos", "panos"):
        add("collect", f"platform {p}", kind="collect", name="x", host="sw", platform=p,
            mode="plain", env=_creds(), commands=["show version"])
    base = ["--name", "r1", "--host", "sw", "--platform", "ios", "--out", "<out>",
            "--timeout", "10"]
    mains = {
        "main ok": dict(mode="plain", env=_creds(), commands=["show version"], argv=base),
        "main config change exits 3": dict(mode="configchange", env=_creds(),
                                           commands=["show version"], argv=base),
        "main refused exits 3": dict(mode="plain", env=_creds(),
                                     commands=["show version", "write memory"], argv=base),
        "main plan": dict(mode="plain", env={}, commands=None, argv=["--plan", "--platform", "ios"]),
        "main plan refused": dict(mode="plain", env={}, commands=None,
                                  argv=["--plan", "--platform", "junos"]),
        "main login rejected": dict(mode="plain", env=_creds(NET_PASSWORD="nope"),
                                    commands=["show version"], argv=base),
    }
    for label, m in mains.items():
        add("collect", label, kind="main", **m)
    return specs


def check_facts_specs(stage, kit):
    """Vectors for conformance/check_facts.py itself: a record that passes,
    and one mutation per rule."""
    specs = []
    name, platform, raw = _fixture("facts/branch1.json")
    good = drivers.build_record(name, platform, raw, collected_at=COLLECTED_AT)
    plan = kit["plans"]["ios"]
    planned = [c for k, c in plan if k == "collect"]
    # the checker never parses raw output, so short stand-ins keep the kit small
    good["raw"] = {c: "(output)" for c in planned if c in raw}
    good["audit"] = {"policy": "ios", "commands": [
        {"time": COLLECTED_AT, "command": c, "kind": k, "status": "ok"} for k, c in plan] + [
        {"time": COLLECTED_AT, "command": "exit", "kind": "session", "status": "sent"}],
        "config_check": {"status": "unchanged", "command": plan[1][1],
                         "before": "! Last configuration change at x",
                         "after": "! Last configuration change at x"}}

    def mutate(fn):
        rec = copy.deepcopy(good)
        fn(rec)
        return rec

    def setkey(path, value):
        def fn(rec):
            target = rec
            for p in path[:-1]:
                target = target[p]
            target[path[-1]] = value
        return fn

    def reorder(rec):
        rec["device"] = dict(reversed(list(rec["device"].items())))

    def drop(path):
        def fn(rec):
            target = rec
            for p in path[:-1]:
                target = target[p]
            del target[path[-1]]
        return fn

    def raw_unplanned(rec):
        rec["raw"]["show running-config"] = "x"

    def raw_order(rec):
        rec["raw"] = dict(reversed(list(rec["raw"].items())))

    def extra_line(rec):
        rec["audit"]["commands"].insert(3, {"time": COLLECTED_AT, "command": "show clock",
                                            "kind": "collect", "status": "ok"})

    def late_enable(rec):
        rec["audit"]["commands"].insert(2, {"time": COLLECTED_AT, "command": "enable",
                                            "kind": "session", "status": "sent"})

    def with_enable(rec):
        rec["audit"]["commands"].insert(0, {"time": COLLECTED_AT, "command": "enable",
                                            "kind": "session", "status": "sent"})

    def timed_out(rec):
        rec["audit"]["commands"] = rec["audit"]["commands"][:4]
        rec["audit"]["commands"][3]["status"] = "timeout"

    def stopped_early(rec):
        rec["audit"]["commands"] = rec["audit"]["commands"][:4]

    def dangling(rec):
        rec["arp"] = [{"ip": "10.0.0.1", "mac": None, "intf": "Gi9/9", "vrf": "default"}]

    def dup(rec):
        rec["interfaces"].append(copy.deepcopy(rec["interfaces"][0]))

    mutations = {
        "good": lambda rec: None,
        "with enable": with_enable,
        "mtu as string": setkey(["interfaces", 0, "mtu"], "1500"),
        "admin_up as 1": setkey(["interfaces", 0, "admin_up"], 1),
        "speed as bool": setkey(["interfaces", 0, "speed_mbps"], True),
        "bad mac": setkey(["interfaces", 0, "mac"], "52:54:00:00:53:0"),
        "upper-case mac": setkey(["interfaces", 0, "mac"], "52:54:00:00:53:0A"),
        "bad cidr": setkey(["interfaces", 0, "ipv4"], ["10.0.0.10"]),
        "bad kind": setkey(["interfaces", 0, "kind"], "ethernet"),
        "extra key": setkey(["device", "site"], "x"),
        "missing key": drop(["interfaces", 0, "vrf"]),
        "keys out of order": reorder,
        "schema 3": setkey(["schema"], 3),
        "time without zone": setkey(["device", "collected_at"], "2026-10-26T01:00:00"),
        "no audit": drop(["audit"]),
        "extra command sent": extra_line,
        "enable after login": late_enable,
        "refused": setkey(["audit", "commands", 2, "status"], "refused"),
        "config changed": setkey(["audit", "config_check", "status"], "changed"),
        "unavailable": setkey(["audit", "config_check", "status"], "unavailable"),
        "timeout stops early": timed_out,
        "stopped early without timeout": stopped_early,
        "unplanned raw": raw_unplanned,
        "raw out of order": raw_order,
        "parse error": setkey(["parse_error"], "ValueError('x')"),
        "no interfaces": setkey(["interfaces"], []),
        "dangling arp interface": dangling,
        "duplicate interface": dup,
        "unknown platform": setkey(["device", "platform"], "junos"),
    }
    for label, fn in mutations.items():
        specs.append(("check_facts", f"check_record[{label}]",
                      dict(kind="call", module="check_facts", func="check_record",
                           args=[mutate(fn), "<kit>", "branch1"])))
    specs.append(("check_facts", "check_record[wrong name]",
                  dict(kind="call", module="check_facts", func="check_record",
                       args=[good, "<kit>", "hq"])))
    specs.append(("check_facts", "check_record[not an object]",
                  dict(kind="call", module="check_facts", func="check_record",
                       args=[[], "<kit>"])))
    other = drivers.build_record(*_fixture("l2/sw1.json"), collected_at=COLLECTED_AT)
    other["raw"] = {c: "(output)" for c in other["raw"]}
    specs.append(("check_facts", "fingerprint", dict(kind="call", module="check_facts",
                                                     func="fingerprint",
                                                     args=[[good, other], "<kit>"])))
    later = mutate(setkey(["device", "collected_at"], "2026-10-27T02:00:00+00:00"))
    specs.append(("check_facts", "compare same", dict(kind="call", module="check_facts",
                                                      func="compare", args=[good, later])))
    specs.append(("check_facts", "compare different",
                  dict(kind="call", module="check_facts", func="compare",
                       args=[good, mutate(setkey(["interfaces", 1, "mtu"], 9000))])))
    return specs


SELFTEST = [
    # Each expectation is deliberately wrong; a runner must report a mismatch
    ("type", dict(kind="call", module="drivers", func="platform_key", args=["ios"]),
     {"value": "IOS"}),
    ("int vs float", dict(kind="attr", module="drivers", name="SCHEMA"),
     {"type": "int", "value": 2.0}),
    ("key order", dict(kind="call", module="drivers.ios", func="parse_show_ip_interface",
                       args=["Vlan10 is up, line protocol is up\n"]),
     {"value": {"Vlan10": {"ipv4": [], "vrf": "default"}}}),
    ("bool vs int", dict(kind="call", module="collect", func="_looks_like_error",
                         args=["% Invalid input\n"]), {"value": 1}),
]


def build(stage):
    kit = {
        "kit": "autotop-awx conformance kit",
        "stage": stage,
        "about": ("Vectors with the exact results of the reference implementation. "
                  "Run with conformance/run_kit.py; see the stage's Copilot instructions."),
        "comparison": "json.dumps(value, ensure_ascii=True, separators=(',', ':')), key order kept",
        "platforms": STAGES[stage]["platforms"],
    }
    with stage_view(stage):
        import collect
        kit["plans"] = {p: run_kit.jsonable(collect.plan(p)) for p in STAGES[stage]["platforms"]}
        kit["facts_schema"] = facts_schema(stage)
        vectors = []
        for section, vid, v in vector_specs(stage) + check_facts_specs(stage, kit):
            v = {"section": section, "id": vid, **v}
            v["expect"] = run_kit.execute(v, kit)
            vectors.append(v)
        for vid, v, wrong in SELFTEST:
            vectors.append({"section": "selftest", "id": vid, **v, "expect_mismatch": True,
                            "expect": wrong})
    kit["vectors"] = vectors
    seen = set()
    for v in vectors:
        if v["id"] in seen:
            raise ValueError(f"duplicate vector id {v['id']!r}")
        seen.add(v["id"])
    return kit


def dumps(kit) -> str:
    """The kit as JSON, one vector per line, so a diff between two kits
    shows which vectors changed."""
    head = {k: v for k, v in kit.items() if k != "vectors"}
    lines = [json.dumps(head, ensure_ascii=True)[:-1] + ', "vectors": [']
    lines.append(",\n".join(json.dumps(v, ensure_ascii=True) for v in kit["vectors"]))
    lines.append("]}")
    return "\n".join(lines) + "\n"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) not in (1, 2) or not argv[0].isdigit() or int(argv[0]) not in STAGES:
        sys.exit(f"usage: build_kit.py <stage: {', '.join(map(str, STAGES))}> [<out.json>]")
    stage = int(argv[0])
    out = Path(argv[1]) if len(argv) == 2 else ROOT / "conformance" / f"stage{stage}" / "kit.json"
    kit = build(stage)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(dumps(kit), encoding="utf-8")
    print(f"wrote {out}: stage {stage}, {len(kit['vectors'])} vectors, "
          f"kit {run_kit.kit_digest(out)}")


if __name__ == "__main__":
    main()
