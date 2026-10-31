"""Link inference from the schema 2 core: point-to-point links, shared
segments drawn as segment nodes, and address reuse across VRFs."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import support
import drivers
import parser as facts_parser


def host(name, *intfs):
    """A minimal schema 2 record. intfs: (name, cidr, vrf, vlan, up)."""
    rec = drivers._empty(name, "ios")
    for intf_name, cidr, vrf, vlan, up in intfs:
        rec["interfaces"].append({
            "name": intf_name, "kind": "physical", "mac": None, "admin_up": True,
            "oper_up": up, "speed_mbps": None, "mtu": None, "description": "",
            "vrf": vrf, "vlan": vlan, "parent": None, "ipv4": [cidr], "ipv6": []})
    return rec


class LinkInferenceTest(unittest.TestCase):
    def test_point_to_point(self):
        edges, segments = facts_parser.extract_links([
            host("r1", ("Gi1", "10.0.0.1/30", "default", None, True)),
            host("r2", ("Gi1", "10.0.0.2/30", "default", None, False))])
        self.assertEqual(segments, [])
        (e,) = edges
        self.assertEqual((e["source"], e["target"], e["source_ip"], e["target_ip"]),
                         ("r1", "r2", "10.0.0.1/30", "10.0.0.2/30"))
        self.assertFalse(e["is_up"])

    def test_shared_segment_becomes_a_node(self):
        edges, segments = facts_parser.extract_links([
            host(f"sw{i}", ("Vlan20", f"10.20.0.{i}/24", "default", 20, True))
            for i in range(1, 5)])
        (seg,) = segments
        self.assertEqual(seg["id"], "segment 10.20.0.0/24")
        self.assertEqual(seg["label"], "VLAN 20 · 10.20.0.0/24")
        self.assertEqual(seg["members"], ["sw1", "sw2", "sw3", "sw4"])
        self.assertEqual(len(edges), 4)  # one per member, not a 6-link mesh
        self.assertTrue(all(e["target"] == seg["id"] and e["target_intf"] == "" for e in edges))

    def test_same_subnet_reused_in_two_vrfs_is_not_one_segment(self):
        # VRF-lite: two separate /30s that happen to use the same addresses
        edges, segments = facts_parser.extract_links([
            host("r1", ("Gi1", "10.9.0.1/30", "RED", None, True)),
            host("r2", ("Gi1", "10.9.0.2/30", "RED", None, True)),
            host("r3", ("Gi1", "10.9.0.1/30", "BLUE", None, True)),
            host("r4", ("Gi1", "10.9.0.2/30", "BLUE", None, True))])
        self.assertEqual(segments, [])
        self.assertEqual({frozenset((e["source"], e["target"])) for e in edges},
                         {frozenset(("r1", "r2")), frozenset(("r3", "r4"))})

    def test_pe_ce_link_across_vrf_names(self):
        # VRF names are local: a PE's customer VRF and the CE's global table
        edges, _ = facts_parser.extract_links([
            host("pe", ("Gi2", "192.0.2.1/30", "CUST", None, True)),
            host("ce", ("Gi0", "192.0.2.2/30", "default", None, True))])
        self.assertEqual([(e["source"], e["target"]) for e in edges], [("pe", "ce")])

    def test_excluded_networks_skipped(self):
        import config
        old = config.EXCLUDED_NETWORKS
        try:
            import ipaddress
            config.EXCLUDED_NETWORKS = [ipaddress.ip_network("10.0.0.0/8")]
            edges, _ = facts_parser.extract_links([
                host("r1", ("Gi1", "10.0.0.1/30", "default", None, True)),
                host("r2", ("Gi1", "10.0.0.2/30", "default", None, True))])
            self.assertEqual(edges, [])
        finally:
            config.EXCLUDED_NETWORKS = old


def run_main(facts_dir, out_dir, template):
    proc = subprocess.run(
        [sys.executable, str(support.SCRIPTS / "main.py"), str(facts_dir), str(out_dir),
         f"--inventory={support.FIXTURES / 'inventory.yml'}", f"--template={template}"],
        capture_output=True, text=True, cwd=out_dir)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    return proc.stdout


class DiagramTest(unittest.TestCase):
    def test_schema_2_facts_draw_the_same_diagram_as_schema_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            outputs = []
            for label, convert in (("v1", False), ("v2", True)):
                facts = tmp / label / "facts"
                facts.mkdir(parents=True)
                for f in support.FACTS.glob("*.json"):
                    rec = json.loads(f.read_text())
                    (facts / f.name).write_text(json.dumps(drivers.load(rec) if convert else rec))
                template = tmp / label / "template.vsdx"
                shutil.copy(support.FIXTURES / "template.vsdx", template)
                run_main(facts, tmp / label, template)
                outputs.append((tmp / label / "topology.drawio").read_text())
            self.assertEqual(outputs[0], outputs[1])

    def test_segment_node_is_drawn_and_its_position_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            facts = tmp / "facts"
            shutil.copytree(support.FACTS, facts)
            # branch1..3 also share a LAN
            for i, name in enumerate(("branch1", "branch2", "branch3"), 1):
                rec = drivers.load(json.loads((facts / f"{name}.json").read_text()))
                rec["interfaces"].append(host(name, ("Vlan50", f"10.50.0.{i}/24", "default", 50,
                                                     True))["interfaces"][0])
                (facts / f"{name}.json").write_text(json.dumps(rec))
            template = tmp / "template.vsdx"
            shutil.copy(support.FIXTURES / "template.vsdx", template)
            out = run_main(facts, tmp, template)
            self.assertIn("1 shared segment(s)", out)

            root = ET.parse(tmp / "topology.drawio").getroot().find(".//root")
            seg = root.find("mxCell[@id='segment 10.50.0.0/24']")
            self.assertIsNotNone(seg)
            self.assertEqual(seg.get("value"), "VLAN 50 · 10.50.0.0/24")
            to_seg = [c for c in root.findall("mxCell")
                      if c.get("edge") == "1" and c.get("target") == seg.get("id")]
            self.assertEqual(len(to_seg), 3)

            # the template now carries the segment; move it and rerun
            support.edit_template_shape(template, "segment 10.50.0.0/24", x=2000, y=900)
            run_main(facts, tmp, template)
            root = ET.parse(tmp / "topology.drawio").getroot().find(".//root")
            geo = root.find("mxCell[@id='segment 10.50.0.0/24']/mxGeometry")
            self.assertEqual((float(geo.get("x")), float(geo.get("y"))), (2000.0, 900.0))


if __name__ == "__main__":
    unittest.main()
