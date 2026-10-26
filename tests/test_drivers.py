import copy
import json
import unittest

import support
import drivers
from drivers import ios


class IosDriverTest(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads((support.FACTS / "core1.json").read_text())
        self.rec = drivers.load(self.raw)

    def test_schema_2_core(self):
        rec = self.rec
        self.assertEqual(rec["schema"], 2)
        self.assertEqual(set(rec), {"schema", "device", "interfaces", "neighbors", "arp",
                                    "mac_table", "facets", "raw"})
        self.assertEqual(rec["device"]["name"], "core1")
        self.assertEqual(rec["device"]["hostname"], "core1")
        self.assertEqual(rec["device"]["vendor"], "cisco")
        self.assertEqual(rec["device"]["platform"], "ios")
        self.assertEqual(rec["device"]["collector"], "ssh-cli")
        gi0 = drivers.interface(rec, "GigabitEthernet0/0")
        self.assertEqual(set(gi0), {"name", "kind", "mac", "admin_up", "oper_up", "speed_mbps",
                                    "mtu", "description", "vrf", "vlan", "parent", "ipv4", "ipv6"})
        self.assertEqual(gi0["kind"], "physical")
        self.assertTrue(gi0["oper_up"])
        self.assertEqual(gi0["speed_mbps"], 1000)
        self.assertEqual(gi0["mtu"], 1500)
        self.assertRegex(gi0["mac"], r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
        self.assertEqual(gi0["ipv4"], ["10.0.0.2/30"])
        self.assertEqual(gi0["vrf"], "default")
        self.assertEqual(rec["raw"], self.raw["cli"])

    def test_admin_down_and_unnumbered(self):
        shut = drivers.interface(self.rec, "GigabitEthernet0/7")
        self.assertFalse(shut["admin_up"])
        self.assertFalse(shut["oper_up"])
        hq = drivers.load(json.loads((support.FACTS / "hq.json").read_text()))
        tun = drivers.interface(hq, "Tunnel1")
        self.assertEqual(tun["kind"], "tunnel")
        self.assertEqual(tun["ipv4"], [])  # unnumbered: no own address

    def test_ipsec_facet(self):
        rec = drivers.load(json.loads((support.FACTS / "hq.json").read_text()))
        tunnels = rec["facets"]["ipsec"]["tunnels"]
        self.assertEqual([t["interface"] for t in tunnels], ["Tunnel1", "Tunnel2", "Tunnel3"])

    def test_secondary_addresses_and_vrf(self):
        out = ios.parse_show_ip_interface(
            "Vlan10 is up, line protocol is up\n"
            "  Internet address is 10.1.1.1/24\n"
            "  Secondary address 10.2.2.1/24\n"
            '  VPN Routing/Forwarding "MGMT"\n')
        self.assertEqual(out["Vlan10"], {"vrf": "MGMT", "ipv4": ["10.1.1.1/24", "10.2.2.1/24"]})

    def test_interface_kinds(self):
        out = ios.parse_show_interfaces(
            "GigabitEthernet1.20 is up, line protocol is up\n"
            "  Encapsulation 802.1Q Virtual LAN, Vlan ID  20.\n"
            "Vlan30 is up, line protocol is up\n"
            "Port-channel1 is up, line protocol is up\n"
            "Loopback0 is up, line protocol is up\n")
        self.assertEqual((out["GigabitEthernet1.20"]["kind"], out["GigabitEthernet1.20"]["parent"],
                          out["GigabitEthernet1.20"]["vlan"]), ("subinterface", "GigabitEthernet1", 20))
        self.assertEqual((out["Vlan30"]["kind"], out["Vlan30"]["vlan"]), ("vlan", 30))
        self.assertEqual(out["Port-channel1"]["kind"], "lag")
        self.assertEqual(out["Loopback0"]["kind"], "loopback")

    def test_c8000v_show_version(self):
        text = (support.FIXTURES / "cli" / "c8000v_show_version.txt").read_text()
        v = ios.parse_show_version(text)
        self.assertEqual(v["hostname"], "AT-8K-1")
        self.assertEqual(v["os_version"], "17.18.02")
        self.assertEqual(v["model"], "C8000V")
        self.assertEqual(v["serial"], "9AAR82D4VBB")
        self.assertEqual(v["role"], "router")

    def test_schema_2_round_trips(self):
        again = drivers.load(json.loads(json.dumps(self.rec)))
        self.assertEqual(again, self.rec)

    def test_reparse_keeps_audit(self):
        rec = copy.deepcopy(self.rec)
        rec["audit"] = {"policy": "ios"}
        rec["interfaces"] = []  # as if written by an older, buggy parser
        fresh = drivers.reparse(rec)
        self.assertEqual(fresh["audit"], {"policy": "ios"})
        self.assertEqual(fresh["interfaces"], self.rec["interfaces"])

    def test_parser_error_keeps_raw(self):
        rec = drivers.build_record("x", "ios", {"show version": 42})  # not text
        self.assertIn("parse_error", rec)
        self.assertEqual(rec["raw"], {"show version": 42})
        self.assertEqual(rec["interfaces"], [])

    def test_legacy_napalm_records_upgrade(self):
        legacy = {"inventory_name": "x", "hostname": "x",
                  "interfaces": {"Gi1": {"is_up": True, "is_enabled": True,
                                         "mac_address": "52:54:00:AA:BB:CC"}},
                  "interfaces_ip": {"Gi1": {"ipv4": {"10.0.0.1": {"prefix_length": 30}}}},
                  "ipsec": {"tunnels": []}}
        rec = drivers.load(legacy)
        gi1 = drivers.interface(rec, "Gi1")
        self.assertEqual(gi1["ipv4"], ["10.0.0.1/30"])
        self.assertTrue(gi1["oper_up"])
        self.assertEqual(gi1["mac"], "52:54:00:aa:bb:cc")
        self.assertEqual(rec["facets"]["ipsec"], {"tunnels": []})

    def test_unknown_platform_is_empty_not_fatal(self):
        rec = drivers.load({"inventory_name": "j", "platform": "junos", "cli": {}})
        self.assertEqual(rec["interfaces"], [])
        self.assertEqual(rec["device"]["name"], "j")

    def test_newer_schema_refused(self):
        with self.assertRaises(ValueError):
            drivers.load({"schema": 3})


if __name__ == "__main__":
    unittest.main()
