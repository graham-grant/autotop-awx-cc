"""Palo Alto firewalls: the PAN-OS driver, the XML API session and its
read-only enforcement (against tests/fake_panos.py, which records every
request it receives), collection directly and through Panorama, the zones
layer, and a mixed Cisco / Palo Alto diagram."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import support
import audit
import collect
import drivers
import parser as facts_parser
import readonly
import xmlapi
from drivers import panos
from fake_panos import KEY, FakePanos
from features.zones import FEATURE as ZONES
from sshcli import AuthError, CliError

FIXTURE = json.loads((support.FIXTURES / "panos" / "pa-fw1.json").read_text())
SERIAL = "007951000123456"
CREDS = {"NET_USERNAME": "admin", "NET_PASSWORD": "secret"}


def quiet(fn, *a, **kw):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return fn(*a, **kw)


class PanosDriverTest(unittest.TestCase):
    def setUp(self):
        self.rec = drivers.load(FIXTURE)

    def test_device(self):
        dev = self.rec["device"]
        self.assertEqual((dev["hostname"], dev["vendor"], dev["platform"], dev["role"],
                          dev["model"], dev["serial"], dev["os_version"], dev["collector"]),
                         ("pa-fw1", "paloalto", "panos", "firewall", "PA-VM", SERIAL,
                          "10.2.10", "xml-api"))

    def test_interfaces(self):
        e1 = drivers.interface(self.rec, "ethernet1/1")
        self.assertEqual((e1["kind"], e1["mac"], e1["oper_up"], e1["speed_mbps"], e1["vrf"],
                          e1["ipv4"]),
                         ("physical", "00:1b:17:00:01:11", True, 1000, "default",
                          ["203.0.113.2/24"]))
        self.assertIsNone(e1["admin_up"])  # not reported by the API
        sub = drivers.interface(self.rec, "ethernet1/4.20")
        self.assertEqual((sub["kind"], sub["parent"], sub["vlan"], sub["oper_up"]),
                         ("subinterface", "ethernet1/4", 20, True))
        # a port with only subinterfaces configured still appears, from <hw>
        self.assertEqual(drivers.interface(self.rec, "ethernet1/4")["mac"], "00:1b:17:00:01:14")
        down = drivers.interface(self.rec, "ethernet1/3")
        self.assertEqual((down["oper_up"], down["ipv4"], down["vrf"]), (False, [], None))
        self.assertEqual(drivers.interface(self.rec, "tunnel.1")["kind"], "tunnel")
        self.assertEqual(drivers.interface(self.rec, "loopback.1")["kind"], "loopback")

    def test_arp_skips_incomplete(self):
        self.assertEqual(self.rec["arp"], [
            {"ip": "10.0.10.1", "mac": "52:54:00:aa:00:01", "intf": "ethernet1/2",
             "vrf": "default"},
            {"ip": "203.0.113.1", "mac": "00:00:5e:00:01:01", "intf": "ethernet1/1",
             "vrf": "default"}])

    def test_lldp(self):
        (n,) = self.rec["neighbors"]
        self.assertEqual(n, {"local_intf": "ethernet1/2", "protocol": "lldp",
                             "remote_name": "sw1.lab.local", "remote_intf": "Gi1/0/2",
                             "remote_mac": "00:a1:b2:c3:01:00", "remote_ip": "10.0.10.2",
                             "remote_platform": None,
                             "remote_capabilities": ["bridge", "router"]})

    def test_ipsec_facet_is_normalised(self):
        up, down = self.rec["facets"]["ipsec"]["tunnels"]
        self.assertEqual((up["interface"], up["local_address"], up["peer_address"],
                          up["status"], up["vendor_status"]),
                         ("tunnel.1", "203.0.113.2", "10.0.10.1", "up", "active"))
        self.assertEqual((down["status"], down["vendor_status"]), ("down", "init"))

    def test_zones_facet(self):
        zones = {z["name"]: z for z in self.rec["facets"]["zones"]["zones"]}
        self.assertEqual(zones["trust"], {"name": "trust", "vsys": "vsys1",
                                          "interfaces": ["ethernet1/2", "loopback.1"]})
        self.assertEqual(sorted(zones), ["dmz", "trust", "untrust", "vpn"])

    def test_config_marker_is_the_newest_commit(self):
        jobs = FIXTURE["cli"][panos.CONFIG_CHECK_COMMAND]
        self.assertEqual(panos.config_marker(jobs), "commit job 41 Commit 2026/10/25 22:10:01")
        # another job finishing isn't a config change
        more = jobs.replace("<job>", "<job><id>50</id><type>Download</type></job><job>", 1)
        self.assertEqual(panos.config_marker(more), panos.config_marker(jobs))
        self.assertEqual(panos.config_marker('<response status="success"><result/></response>'),
                         "no commit jobs")

    def test_every_request_is_read_only(self):
        policy = readonly.policy_for("panos")
        for request in (panos.CORE_COMMANDS + drivers.facet_commands("panos")
                        + [panos.CONFIG_CHECK_COMMAND, xmlapi.KEYGEN,
                           xmlapi.CONNECTED_DEVICES]):
            self.assertTrue(policy.allows(request), request)


class ApiSessionTest(unittest.TestCase):
    def session(self, api, **kw):
        logged = []
        kw.setdefault("policy", readonly.policy_for("panos"))
        s = xmlapi.ApiSession("127.0.0.1", "admin", kw.pop("password", "secret"),
                              port=api.port, scheme="http", log=logged.append, **kw)
        s.logged = logged
        return s

    def test_login_then_requests_key_never_recorded(self):
        with FakePanos(FIXTURE) as api, self.session(api) as s:
            out = s.run(panos.SYSTEM_INFO)
            self.assertIn("<hostname>pa-fw1</hostname>", out)
        self.assertEqual([r["type"] for r in api.received], ["keygen", "op"])
        self.assertEqual(api.received[1]["key"], "****")  # sent, in the body
        self.assertEqual([r["query"] for r in api.received], ["", ""])  # never in a URL
        self.assertEqual([(a["command"], a["kind"], a["status"]) for a in s.audit],
                         [("type=keygen", "session", "ok"),
                          (panos.SYSTEM_INFO, "collect", "ok")])
        text = json.dumps(s.audit) + "\n".join(s.logged)
        self.assertNotIn(KEY, text)
        self.assertNotIn("secret", text)

    def test_refused_request_never_reaches_the_firewall(self):
        with FakePanos(FIXTURE) as api, self.session(api) as s:
            for bad in ("type=op&cmd=<request><restart><system></system></restart></request>",
                        "type=config&action=set&xpath=/config/x&element=<y/>",
                        "type=commit&cmd=<commit></commit>", "type=keygen"):
                with self.subTest(request=bad), self.assertRaises(readonly.CommandRefused):
                    s.run(bad)
        self.assertEqual([r["type"] for r in api.received], ["keygen"])
        self.assertEqual([a["status"] for a in s.audit[1:]], ["refused"] * 4)

    def test_no_policy_sends_nothing(self):
        with FakePanos(FIXTURE) as api:
            with self.assertRaises(readonly.CommandRefused):
                self.session(api, policy=None).open()
        self.assertEqual(api.received, [])

    def test_wrong_password_is_masked(self):
        with FakePanos(FIXTURE) as api:
            with self.assertRaises(AuthError) as cm:
                self.session(api, password="hunter2").open()
        self.assertIn("Invalid Credential", str(cm.exception))
        self.assertNotIn("hunter2", str(cm.exception))

    def test_device_error_is_a_skippable_error(self):
        with FakePanos(FIXTURE) as api, self.session(api) as s:
            with self.assertRaises(CliError):
                s.run("type=op&cmd=<show><bogus></bogus></show>")
        self.assertEqual(s.audit[-1]["status"], "error")

    def test_plain_http_only_to_localhost(self):
        with self.assertRaises(CliError):
            xmlapi.ApiSession("192.0.2.1", "admin", "secret", scheme="http")

    def test_panorama_target_on_every_request(self):
        with FakePanos(FIXTURE, serial=SERIAL) as api, self.session(api) as s:
            s.target = s.find_serial("PA-FW1")
            self.assertEqual(s.target, SERIAL)
            s.run(panos.INTERFACES)
        self.assertNotIn("target", api.received[1])  # the lookup asks Panorama itself
        self.assertEqual(api.received[2]["target"], SERIAL)
        self.assertEqual(s.audit[-1]["command"], f"{panos.INTERFACES}&target={SERIAL}")
        self.assertTrue(readonly.policy_for("panos").allows(s.audit[-1]["command"]))

    def test_panorama_unknown_firewall(self):
        with FakePanos(FIXTURE, serial=SERIAL) as api, self.session(api) as s:
            with self.assertRaises(CliError):
                s.find_serial("pa-fw9")


@unittest.skipUnless(shutil.which("openssl"), "needs the openssl command")
class TlsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.cert, cls.key = str(d / "cert.pem"), str(d / "key.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                        "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1",
                        "-keyout", cls.key, "-out", cls.cert],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def open(self, api, **kw):
        s = xmlapi.ApiSession("127.0.0.1", "admin", "secret", port=api.port,
                              policy=readonly.policy_for("panos"), log=lambda _: None, **kw)
        return s.open()

    def test_certificate_verified_by_default(self):
        with FakePanos(FIXTURE, tls=(self.cert, self.key)) as api:
            with self.assertRaises(CliError) as cm:
                self.open(api)
            self.assertIn("CERTIFICATE_VERIFY_FAILED", str(cm.exception))
            self.assertEqual(api.received, [])  # nothing sent, password included

    def test_ca_file_or_explicit_opt_out(self):
        with FakePanos(FIXTURE, tls=(self.cert, self.key)) as api:
            self.assertIsNotNone(self.open(api, ca_file=self.cert)._key)
            self.assertIsNotNone(self.open(api, verify=False)._key)


class CollectPanosTest(unittest.TestCase):
    def collect(self, api, **kw):
        with mock.patch.dict(os.environ, CREDS):
            return quiet(collect.collect, "pa-fw1", "127.0.0.1", "panos", port=api.port,
                         scheme="http", **kw)

    def test_direct(self):
        with FakePanos(FIXTURE) as api:
            rec = self.collect(api)
        self.assertEqual(rec["audit"]["config_check"]["status"], "unchanged")
        self.assertEqual(sorted(rec["raw"]), sorted(collect.commands("panos")))
        self.assertEqual(rec["device"]["serial"], SERIAL)
        sent = [a["command"] for a in rec["audit"]["commands"]]
        self.assertEqual(len(sent), len(api.received))  # every request audited
        self.assertEqual(audit.verify(rec), ([], []))

    def test_through_panorama(self):
        with FakePanos(FIXTURE, serial=SERIAL) as api:
            rec = self.collect(api, panorama="127.0.0.1")
        self.assertEqual(rec["audit"]["via_panorama"], {"host": "127.0.0.1", "target": SERIAL})
        self.assertEqual(rec["interfaces"], drivers.load(FIXTURE)["interfaces"])
        targets = [r.get("target") for r in api.received]
        self.assertEqual(targets[:2], [None, None])  # keygen, serial lookup
        self.assertEqual(set(targets[2:]), {SERIAL})
        self.assertEqual(audit.verify(rec), ([], []))

    def test_panorama_with_serial_skips_lookup(self):
        with FakePanos(FIXTURE, serial=SERIAL) as api:
            self.collect(api, panorama="127.0.0.1", serial=SERIAL)
        self.assertNotIn("<connected>", " ".join(r.get("cmd", "") for r in api.received))

    def test_panos_credentials_preferred(self):
        with FakePanos(FIXTURE, username="pa-ro", password="pa-secret") as api, \
                mock.patch.dict(os.environ, {"PANOS_USERNAME": "pa-ro",
                                             "PANOS_PASSWORD": "pa-secret"}):
            rec = self.collect(api)
        self.assertEqual(rec["audit"]["commands"][0]["status"], "ok")

    def test_config_change_fails_the_host(self):
        with tempfile.TemporaryDirectory() as tmp, FakePanos(FIXTURE, config_change=True) as api, \
                mock.patch.dict(os.environ, CREDS):
            out = Path(tmp) / "pa-fw1.json"
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err), \
                    self.assertRaises(SystemExit) as cm:
                collect.main(["--name", "pa-fw1", "--host", "127.0.0.1", "--platform", "panos",
                              "--port", str(api.port), "--scheme", "http", "--out", str(out)])
            self.assertEqual(cm.exception.code, 3)
            self.assertIn("CONFIGURATION CHANGED", err.getvalue())
            rec = json.loads(out.read_text())
            self.assertEqual(rec["audit"]["config_check"]["after"],
                             "commit job 99 Commit 2026/10/26 01:00:05")
            self.assertTrue(audit.verify(rec)[0])

    def test_audit_flags_a_late_login(self):
        with FakePanos(FIXTURE) as api:
            rec = self.collect(api)
        rec["audit"]["commands"].append(dict(rec["audit"]["commands"][0]))
        self.assertTrue(audit.verify(rec)[0])

    def test_panorama_option_only_for_panos(self):
        with self.assertRaises(CliError):
            quiet(collect.collect, "sw1", "127.0.0.1", "ios", panorama="127.0.0.1")


class ZonesLayerTest(unittest.TestCase):
    def test_port_label(self):
        facet = {"zones": [{"name": "untrust", "vsys": "vsys1", "interfaces": ["ethernet1/1"]},
                           {"name": "dmz", "vsys": "vsys1", "interfaces": ["ethernet1/4.20"]},
                           {"name": "guest", "vsys": "vsys1", "interfaces": ["ethernet1/4.30"]}]}
        text, tip, style = ZONES.port_label(facet, "ethernet1/1")
        self.assertEqual((text, style), ("untrust", {"fill": "#F8CECC"}))
        self.assertEqual(ZONES.port_label(facet, "ethernet1/4")[:2],
                         ("dmz, guest",
                          "ethernet1/4.20: zone dmz (vsys1); ethernet1/4.30: zone guest (vsys1)"))
        self.assertIsNone(ZONES.port_label(facet, "ethernet1/9"))
        self.assertIsNone(ZONES.port_label(facet, "ethernet1/10"))  # not a prefix match
        self.assertEqual(ZONES.color("dmz"), ZONES.color("dmz"))


class MixedDiagramTest(unittest.TestCase):
    """The layer 2 lab plus the firewall, cabled to sw1 Gi1/0/2."""

    def test_firewall_in_the_lab(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            facts = tmp / "facts"
            shutil.copytree(support.FIXTURES / "l2", facts)
            shutil.copy(support.FIXTURES / "panos" / "pa-fw1.json", facts)
            sw1 = drivers.load(json.loads((facts / "sw1.json").read_text()))
            sw1["interfaces"].append(dict(drivers.interface(sw1, "GigabitEthernet1/0/5"),
                                          name="GigabitEthernet1/0/2", mac="00:a1:b2:c3:01:02"))
            (facts / "sw1.json").write_text(json.dumps(sw1))
            (tmp / "inventory.yml").write_text("all:\n  hosts:\n" + "".join(
                f"    {n}: {{}}\n" for n in ("rtr1", "sw1", "sw2", "srv1", "srv2", "pa-fw1")))

            edges, _ = facts_parser.extract_links(facts_parser.load_nodes(facts))
            (fw,) = [e for e in edges if "pa-fw1" in (e["source"], e["target"])]
            self.assertEqual((fw["source"], fw["source_intf"], fw["target"], fw["target_intf"],
                              fw["evidence"], fw["source_ip"]),
                             ("pa-fw1", "ethernet1/2", "sw1", "GigabitEthernet1/0/2",
                              ["lldp", "subnet"], "10.0.10.254/24"))

            proc = subprocess.run(
                [sys.executable, str(support.SCRIPTS / "main.py"), str(facts), str(tmp),
                 f"--inventory={tmp / 'inventory.yml'}", f"--template={tmp / 'template.drawio'}"],
                capture_output=True, text=True, cwd=tmp)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            root = ET.parse(tmp / "topology.drawio").getroot().find(".//root")
            objs = {o.get("id"): o for o in root.findall("object")}
            zone = objs[f"zones-{fw['id']}-source"]
            self.assertEqual(zone.get("label"), "trust")
            self.assertEqual(zone.find("mxCell").get("parent"), "layer-zones")
            (tunnel,) = [o for o in objs.values() if o.get("id", "").startswith("ipsec-")]
            cell = tunnel.find("mxCell")
            self.assertEqual({cell.get("source"), cell.get("target")}, {"pa-fw1", "rtr1"})
            self.assertIn("up", tunnel.get("label") + tunnel.get("tooltip", ""))


class StagingGridTest(unittest.TestCase):
    def test_staged_nodes_never_overlap(self):
        import config
        import main
        names = [f"n{i}" for i in range(7)]
        sizes = {n: (config.DEFAULT_NODE_W, config.DEFAULT_NODE_H) for n in names}
        sizes["n3"] = (400, 90)
        pos = main._place_unpinned_nodes({}, names, sizes)
        for a in names:
            for b in names:
                if a < b:
                    (ax, ay), (bx, by) = pos[a], pos[b]
                    apart_x = abs(ax - bx) >= (sizes[a][0] + sizes[b][0]) / 2
                    apart_y = abs(ay - by) >= (sizes[a][1] + sizes[b][1]) / 2
                    self.assertTrue(apart_x or apart_y, (a, b))


if __name__ == "__main__":
    unittest.main()
