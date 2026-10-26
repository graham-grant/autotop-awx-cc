"""Layer 2: the Cisco neighbor, ARP, MAC table and VLAN parsers, links from
LLDP/CDP and MAC table evidence, port-channel collapse, and the VLANs layer.

The fixtures in fixtures/l2/ are a router (rtr1), two switches joined by a
two-member port-channel (sw1, sw2) and two servers (srv1 on sw1, srv2 on
sw2) that only show up in the switches' MAC tables."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import support
import config
import drivers
import parser as facts_parser
from drivers import ios
from drivers.common import canonical_intf, norm_mac
from features.vlans import FEATURE as VLANS

L2 = support.FIXTURES / "l2"
L2_INVENTORY = support.FIXTURES / "l2-inventory.yml"


def raw(name):
    return json.loads((L2 / f"{name}.json").read_text())["cli"]


def load_l2():
    return facts_parser.load_nodes(L2)


def summary(edges):
    return {(e["source"], e["source_intf"], e["target"], e["target_intf"]):
            (e["evidence"], e["members"]) for e in edges}


class CommonHelpersTest(unittest.TestCase):
    def test_norm_mac(self):
        for text in ("5254.0012.3456", "52-54-00-12-34-56", "52:54:00:12:34:56",
                     "5254.0012.3456 "):
            self.assertEqual(norm_mac(text), "52:54:00:12:34:56")
        for text in (None, "", "Gi1/0/1", "5254.0012.345", "sw1.lab.local"):
            self.assertIsNone(norm_mac(text))

    def test_canonical_intf(self):
        known = ["GigabitEthernet1/0/1", "GigabitEthernet1/0/11", "Port-channel1",
                 "TenGigabitEthernet1/1/1"]
        self.assertEqual(canonical_intf("Gi1/0/1", known), "GigabitEthernet1/0/1")
        self.assertEqual(canonical_intf("Po1", known), "Port-channel1")
        self.assertEqual(canonical_intf("port-channel1", known), "Port-channel1")
        self.assertEqual(canonical_intf("Te1/1/1", known), "TenGigabitEthernet1/1/1")
        self.assertEqual(canonical_intf("Gi9/9", known), "Gi9/9")  # unknown: unchanged
        # ambiguous prefix: unchanged
        self.assertEqual(canonical_intf("G1/0/1", known + ["GigE1/0/1"]), "G1/0/1")


class CiscoL2ParserTest(unittest.TestCase):
    def test_cdp(self):
        (n,) = ios.parse_cdp_neighbors(raw("sw1")["show cdp neighbors detail"])
        self.assertEqual(n, {"local_intf": "GigabitEthernet1/0/1", "protocol": "cdp",
                             "remote_name": "rtr1", "remote_intf": "GigabitEthernet1",
                             "remote_mac": None, "remote_ip": "10.0.10.1",
                             "remote_platform": "cisco C8000V",
                             "remote_capabilities": ["router"]})

    def test_cdp_strips_serial_from_device_id(self):
        text = ("-------------------------\nDevice ID: sw9(FOC1234X0AB)\n"
                "Platform: cisco WS-C3850,  Capabilities: Switch IGMP\n"
                "Interface: Gi1/0/2,  Port ID (outgoing port): Gi1/0/48\n")
        (n,) = ios.parse_cdp_neighbors(text)
        self.assertEqual((n["remote_name"], n["remote_capabilities"]), ("sw9", ["bridge"]))

    def test_lldp(self):
        a, b = ios.parse_lldp_neighbors(raw("sw1")["show lldp neighbors detail"])
        self.assertEqual((a["local_intf"], a["remote_name"], a["remote_intf"], a["remote_ip"]),
                         ("Gi1/0/23", "sw2.lab.local", "Gi1/0/23", "10.0.10.3"))
        self.assertEqual(a["remote_mac"], "00:a1:b2:c4:01:00")  # chassis id
        self.assertEqual(a["remote_capabilities"], ["bridge", "router"])
        self.assertEqual(b["local_intf"], "Gi1/0/24")

    def test_lldp_mac_port_id_becomes_remote_mac(self):
        text = ("------------------------------------------------\n"
                "Local Intf: Gi1/0/9\nChassis id: 0050.5601.0099\nPort id: 0050.5601.0099\n"
                "Port Description: Ethernet0\nSystem Name: srv9\n"
                "Enabled Capabilities: S\n")
        (n,) = ios.parse_lldp_neighbors(text)
        self.assertEqual((n["remote_intf"], n["remote_mac"], n["remote_capabilities"]),
                         ("Ethernet0", "00:50:56:01:00:99", ["station"]))

    def test_arp(self):
        text = ("Protocol  Address          Age (min)  Hardware Addr   Type   Interface\n"
                "Internet  10.0.10.1               -   5254.00aa.0001  ARPA   GigabitEthernet1\n"
                "Internet  10.0.10.11              3   0050.5601.0011  ARPA   GigabitEthernet1\n"
                "Internet  10.0.10.99              0   Incomplete      ARPA\n")
        self.assertEqual(ios.parse_arp(text), [
            {"ip": "10.0.10.1", "mac": "52:54:00:aa:00:01", "intf": "GigabitEthernet1",
             "vrf": "default"},
            {"ip": "10.0.10.11", "mac": "00:50:56:01:00:11", "intf": "GigabitEthernet1",
             "vrf": "default"}])

    def test_mac_table_skips_cpu_entries(self):
        text = (raw("sw1")["show mac address-table dynamic"]
                + "\n All    0100.0ccc.cccc    STATIC      CPU\n")
        entries = ios.parse_mac_table(text)
        self.assertEqual(len(entries), 4)
        self.assertEqual(entries[2], {"mac": "00:50:56:01:00:12", "intf": "Po1", "vlan": 10})

    def test_vlan_brief(self):
        vlans = ios.parse_vlan_brief(raw("sw1")["show vlan brief"])
        self.assertEqual([v["id"] for v in vlans], [1, 10, 20])  # 1002-1005 left out
        self.assertEqual(vlans[1], {"id": 10, "name": "SERVERS",
                                    "ports": ["Gi1/0/1", "Gi1/0/5"]})
        self.assertEqual(vlans[2]["ports"], [])

    def test_vlan_brief_continuation_lines(self):
        text = ("VLAN Name                             Status    Ports\n"
                "---- -------------------------------- --------- ------------------------\n"
                "1    default                          active    Gi1/0/1, Gi1/0/2\n"
                "                                                Gi1/0/3, Gi1/0/4\n")
        (v,) = ios.parse_vlan_brief(text)
        self.assertEqual(v["ports"], ["Gi1/0/1", "Gi1/0/2", "Gi1/0/3", "Gi1/0/4"])

    def test_interfaces_trunk(self):
        self.assertEqual(ios.parse_interfaces_trunk(raw("sw1")["show interfaces trunk"]),
                         [{"port": "Po1", "native": 1, "allowed": "1-4094",
                           "active": "1,10,20"}])

    def test_core_and_facet_use_full_interface_names(self):
        rec = drivers.load({"inventory_name": "sw1", "platform": "ios", "cli": raw("sw1")})
        self.assertEqual(drivers.interface(rec, "GigabitEthernet1/0/23")["parent"],
                         "Port-channel1")
        self.assertIsNone(drivers.interface(rec, "GigabitEthernet1/0/5")["parent"])
        self.assertEqual({n["local_intf"] for n in rec["neighbors"]},
                         {"GigabitEthernet1/0/1", "GigabitEthernet1/0/23",
                          "GigabitEthernet1/0/24"})
        self.assertEqual({m["intf"] for m in rec["mac_table"]},
                         {"GigabitEthernet1/0/1", "GigabitEthernet1/0/5", "Port-channel1"})
        vlans = rec["facets"]["vlans"]
        self.assertEqual(vlans["trunks"][0]["port"], "Port-channel1")
        self.assertIn("GigabitEthernet1/0/5", vlans["vlans"][1]["ports"])

    def test_new_commands_are_read_only(self):
        import readonly
        policy = readonly.policy_for("ios")
        for cmd in ios.CORE_COMMANDS + ios.FACET_COMMANDS["vlans"]:
            self.assertTrue(policy.allows(cmd), cmd)


def host(name, intfs=(), mac_table=(), neighbors=()):
    """A schema 2 record. intfs: (name, mac, cidr or None[, parent]).
    Names starting "Po" are port-channels."""
    rec = drivers._empty(name, "ios")
    for intf_name, mac, cidr, *parent in intfs:
        rec["interfaces"].append({
            "name": intf_name, "kind": "lag" if intf_name.startswith("Po") else "physical",
            "mac": mac, "admin_up": True, "oper_up": True, "speed_mbps": None, "mtu": None,
            "description": "", "vrf": "default", "vlan": None,
            "parent": parent[0] if parent else None,
            "ipv4": [cidr] if cidr else [], "ipv6": []})
    rec["mac_table"] = [{"mac": m, "intf": i, "vlan": 1} for m, i in mac_table]
    rec["neighbors"] = list(neighbors)
    return rec


def nb(local, name, port, proto="lldp"):
    return {"local_intf": local, "protocol": proto, "remote_name": name,
            "remote_intf": port, "remote_mac": None, "remote_ip": None,
            "remote_platform": None, "remote_capabilities": []}


def mclag_peer(name, mac_table=(), neighbors=()):
    """One of a pair of MC-LAG switches: Gi1/0/10 in Po10 faces the bundle."""
    return host(name, [("Gi1/0/10", None, None, "Po10"), ("Po10", None, None),
                       ("Gi1/0/48", None, None)], mac_table, neighbors)


class EvidenceTest(unittest.TestCase):
    def test_fixture_links(self):
        edges, segments = facts_parser.extract_links(load_l2())
        self.assertEqual(segments, [])  # the shared /24 is already joined at layer 2
        self.assertEqual(summary(edges), {
            ("rtr1", "GigabitEthernet1", "sw1", "GigabitEthernet1/0/1"): (["cdp", "subnet"], 1),
            ("sw1", "Port-channel1", "sw2", "Port-channel1"): (["lldp", "subnet"], 2),
            ("sw1", "GigabitEthernet1/0/5", "srv1", "Ethernet0"): (["mac", "subnet"], 1),
            ("sw2", "GigabitEthernet1/0/7", "srv2", "Ethernet0"): (["mac", "subnet"], 1)})
        rtr = edges[0]
        self.assertEqual((rtr["source_ip"], rtr["target_ip"]), ("10.0.10.1/24", ""))

    def test_uplink_macs_ignored(self):
        # srv2's MAC is also learned on sw1's Po1, which faces sw2
        edges, _ = facts_parser.extract_links(load_l2())
        self.assertFalse([e for e in edges if {e["source"], e["target"]} == {"sw1", "srv2"}])

    def test_evidence_filter(self):
        old = config.LINK_EVIDENCE
        try:
            config.LINK_EVIDENCE = ("lldp", "cdp")
            edges, _ = facts_parser.extract_links(load_l2())
            self.assertEqual({e["evidence"][0] for e in edges}, {"lldp", "cdp"})
            self.assertEqual(len(edges), 2)
            config.LINK_EVIDENCE = ("subnet",)
            edges, segments = facts_parser.extract_links(load_l2())
            self.assertEqual(edges[0]["evidence"], ["subnet"])
            self.assertEqual(len(segments), 1)  # back to a segment for the /24
        finally:
            config.LINK_EVIDENCE = old

    def test_port_with_two_devices_is_not_a_link(self):
        # an unmanaged switch behind Gi1/0/2 with both servers on it
        edges, _ = facts_parser.extract_links([
            host("sw", [("Gi1/0/2", "aa:00:00:00:00:01", None)],
                 mac_table=[("00:50:56:00:00:01", "Gi1/0/2"), ("00:50:56:00:00:02", "Gi1/0/2")]),
            host("a", [("eth0", "00:50:56:00:00:01", None)]),
            host("b", [("eth0", "00:50:56:00:00:02", None)])])
        self.assertEqual(edges, [])

    def test_equal_ports_skipped_fewest_macs_wins(self):
        two_ports = [("00:50:56:00:00:01", "Gi1/0/2"), ("00:50:56:00:00:01", "Gi1/0/3")]
        edges, _ = facts_parser.extract_links([
            host("sw", [("Gi1/0/2", None, None), ("Gi1/0/3", None, None)],
                 mac_table=two_ports),
            host("a", [("eth0", "00:50:56:00:00:01", None)])])
        self.assertEqual(edges, [])
        # one of the ports also carries an unknown MAC: the other one wins
        edges, _ = facts_parser.extract_links([
            host("sw", [("Gi1/0/2", None, None), ("Gi1/0/3", None, None)],
                 mac_table=two_ports + [("00:50:56:00:99:99", "Gi1/0/3")]),
            host("a", [("eth0", "00:50:56:00:00:01", None)])])
        self.assertEqual([(e["source_intf"], e["evidence"]) for e in edges],
                         [("Gi1/0/2", ["mac"])])

    def test_neighbor_seen_from_both_ends_is_one_link(self):
        edges, _ = facts_parser.extract_links([
            host("r1", [("Gi1", None, None)], neighbors=[nb("Gi1", "r2.lab", "Gi2", "cdp")]),
            host("r2", [("Gi2", None, None)], neighbors=[nb("Gi2", "r1", "Gi1", "lldp")])])
        self.assertEqual(summary(edges), {("r1", "Gi1", "r2", "Gi2"): (["lldp", "cdp"], 1)})

    def test_ties_on_one_switch_or_on_plain_ports_skipped(self):
        mac = "00:50:56:00:00:01"
        # two port-channels on the same switch: not an MC-LAG
        edges, _ = facts_parser.extract_links([
            host("sw", [("Po1", None, None), ("Po2", None, None)],
                 mac_table=[(mac, "Po1"), (mac, "Po2")]),
            host("a", [("eth0", mac, None)])])
        self.assertEqual(edges, [])
        # access ports on two switches: not a bundle either
        edges, _ = facts_parser.extract_links([
            host("swA", [("Gi1/0/2", None, None)], mac_table=[(mac, "Gi1/0/2")]),
            host("swB", [("Gi1/0/2", None, None)], mac_table=[(mac, "Gi1/0/2")]),
            host("a", [("eth0", mac, None)])])
        self.assertEqual(edges, [])

    def test_old_facts_without_layer_2_data_unchanged(self):
        # the phase 1 lab fixtures have no neighbors or MAC tables
        nodes = facts_parser.load_nodes(support.FACTS)
        edges, _ = facts_parser.extract_links(nodes)
        self.assertTrue(edges)
        self.assertTrue(all(e["evidence"] == ["subnet"] for e in edges))


class McLagTest(unittest.TestCase):
    """A port-channel whose members end on two switches (vPC, MLAG, VSX)."""

    def test_neighbors_give_one_link_per_peer(self):
        acc = host("acc", [("Gi1/0/1", None, None, "Po1"), ("Gi1/0/2", None, None, "Po1"),
                           ("Po1", None, None)],
                   neighbors=[nb("Gi1/0/1", "swA", "Gi1/0/10"),
                              nb("Gi1/0/2", "swB", "Gi1/0/10")])
        edges, _ = facts_parser.extract_links([
            acc, mclag_peer("swA", neighbors=[nb("Gi1/0/10", "acc", "Gi1/0/1")]),
            mclag_peer("swB", neighbors=[nb("Gi1/0/10", "acc", "Gi1/0/2")])])
        self.assertEqual(summary(edges), {("acc", "Po1", "swA", "Po10"): (["lldp"], 1),
                                          ("acc", "Po1", "swB", "Po10"): (["lldp"], 1)})
        self.assertEqual([e["mclag"] for e in edges], [["source"], ["source"]])

    def test_ordinary_port_channel_is_not_mclag(self):
        edges, _ = facts_parser.extract_links(load_l2())
        self.assertFalse([e for e in edges if "mclag" in e])

    def test_bonded_server_found_by_mac_on_both_peers(self):
        mac = "00:50:56:00:00:01"
        edges, _ = facts_parser.extract_links([
            host("srv", [("bond0", mac, None)]),
            mclag_peer("swA", mac_table=[(mac, "Gi1/0/10")]),
            mclag_peer("swB", mac_table=[(mac, "Po10")])])
        self.assertEqual(summary(edges), {("swA", "Po10", "srv", "bond0"): (["mac"], 1),
                                          ("swB", "Po10", "srv", "bond0"): (["mac"], 1)})
        self.assertEqual([e["mclag"] for e in edges], [["target"], ["target"]])

    def test_labelled_on_the_diagram(self):
        mac = "00:50:56:00:00:01"
        records = [host("srv", [("bond0", mac, None)]),
                   mclag_peer("swA", mac_table=[(mac, "Po10")]),
                   mclag_peer("swB", mac_table=[(mac, "Po10")])]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "facts").mkdir()
            for rec in records:
                (tmp / "facts" / f"{rec['device']['name']}.json").write_text(json.dumps(rec))
            (tmp / "inventory.yml").write_text(
                "all:\n  hosts:\n    srv: {}\n    swA: {}\n    swB: {}\n")
            proc = subprocess.run(
                [sys.executable, str(support.SCRIPTS / "main.py"), str(tmp / "facts"),
                 str(tmp), f"--inventory={tmp / 'inventory.yml'}",
                 f"--template={tmp / 'template.drawio'}"],
                capture_output=True, text=True, cwd=tmp)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            values = [c.get("value") for c in
                      ET.parse(tmp / "topology.drawio").getroot().iter("mxCell")]
            self.assertEqual(values.count("bond0 MC-LAG"), 2)
            self.assertEqual(values.count("Po10"), 2)


class VlansLayerTest(unittest.TestCase):
    def test_port_labels(self):
        facet = {"vlans": [{"id": 20, "name": "USERS", "ports": ["Gi1/0/5"]}],
                 "trunks": [{"port": "Po1", "native": 1, "allowed": "1-4094", "active": ""},
                            {"port": "Po2", "native": 1, "allowed": "1-4094",
                             "active": "1,10,20,30,40,50,60,70,80,90,100"}]}
        self.assertEqual(VLANS.port_label(facet, "Gi1/0/5")[0], "VLAN 20")
        self.assertEqual(VLANS.port_label(facet, "Po1")[0], "trunk 1-4094")
        self.assertEqual(VLANS.port_label(facet, "Po2")[0], "trunk 1,10,20,30,40,50,60,...")
        self.assertIsNone(VLANS.port_label(facet, "Gi1/0/9"))

    def test_rendered_from_fixtures(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            proc = subprocess.run(
                [sys.executable, str(support.SCRIPTS / "main.py"), str(L2), str(tmp),
                 f"--inventory={L2_INVENTORY}", f"--template={tmp / 'template.drawio'}"],
                capture_output=True, text=True, cwd=tmp)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn("feature vlans: ok", proc.stdout)
            root = ET.parse(tmp / "topology.drawio").getroot().find(".//root")

            labels = {o.get("id"): o.get("label") for o in root.findall("object")
                      if o.find("mxCell").get("parent") == "layer-vlans"}
            self.assertEqual(sorted(labels.values()),
                             ["VLAN 10", "VLAN 10", "VLAN 10", "trunk 1,10,20", "trunk 1,10,20"])

            edges = {c.get("id"): c for c in root.findall("mxCell") if c.get("edge") == "1"}
            dashed = sorted(i for i, c in edges.items() if "dashed=1" in c.get("style"))
            by_id = {e["id"]: e for e in facts_parser.extract_links(load_l2())[0]}
            self.assertEqual([by_id[i]["target"] for i in dashed], ["srv1", "srv2"])
            values = [c.get("value") for c in root.iter("mxCell")]
            self.assertEqual(values.count("Po1 ×2"), 2)


if __name__ == "__main__":
    unittest.main()
