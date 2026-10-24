import json
import unittest

import support
import netparse
from netparse import ios


class IosParserTest(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads((support.FACTS / "core1.json").read_text())

    def test_normalize_matches_napalm_shape(self):
        rec = netparse.normalize(self.raw)
        self.assertEqual(rec["hostname"], "core1")
        gi0 = rec["interfaces"]["GigabitEthernet0/0"]
        self.assertEqual(set(gi0), {"is_up", "is_enabled", "description", "last_flapped",
                                    "speed", "mtu", "mac_address"})
        self.assertTrue(gi0["is_up"])
        self.assertEqual(gi0["speed"], 1000.0)
        self.assertEqual(gi0["mtu"], 1500)
        self.assertRegex(gi0["mac_address"], r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")
        self.assertEqual(rec["interfaces_ip"]["GigabitEthernet0/0"],
                         {"ipv4": {"10.0.0.2": {"prefix_length": 30}}})
        self.assertIn("cli", rec)  # kept for feature plugins

    def test_admin_down_and_unnumbered(self):
        rec = netparse.normalize(self.raw)
        shut = rec["interfaces"]["GigabitEthernet0/7"]
        self.assertFalse(shut["is_enabled"])
        self.assertFalse(shut["is_up"])
        self.assertNotIn("Tunnel1", rec["interfaces_ip"])  # unnumbered: no own address

    def test_secondary_addresses(self):
        out = ios.parse_show_ip_interface(
            "Vlan10 is up, line protocol is up\n"
            "  Internet address is 10.1.1.1/24\n"
            "  Secondary address 10.2.2.1/24\n")
        self.assertEqual(out["Vlan10"]["ipv4"], {"10.1.1.1": {"prefix_length": 24},
                                                 "10.2.2.1": {"prefix_length": 24}})

    def test_c8000v_show_version_hostname(self):
        text = (support.FIXTURES / "cli" / "c8000v_show_version.txt").read_text()
        self.assertEqual(ios.parse_show_version(text), "AT-8K-1")

    def test_legacy_napalm_records_pass_through(self):
        legacy = {"inventory_name": "x", "hostname": "x",
                  "interfaces": {"Gi1": {"is_up": True}}, "interfaces_ip": {}}
        self.assertEqual(netparse.normalize(legacy), legacy)

    def test_unknown_platform_is_empty_not_fatal(self):
        rec = netparse.normalize({"inventory_name": "j", "platform": "junos", "cli": {}})
        self.assertEqual(rec["interfaces"], {})
        self.assertEqual(rec["interfaces_ip"], {})


if __name__ == "__main__":
    unittest.main()
