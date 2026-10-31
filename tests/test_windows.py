"""Windows servers: the PowerShell scripts and driver, the PowerShell-over-
OpenSSH session against tests/fake_winssh.py (which records the decoded
script it was sent), collection and audit, placement by switch MAC tables
with an NIC team, and the server roles layer."""
import base64
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
from drivers import windows
from features.roles import FEATURE as ROLES
from sshcli import AuthError, PowerShellSession

FIXTURE_PATH = support.FIXTURES / "windows" / "srv-win1.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text())
FAKE = str(support.ROOT / "tests" / "fake_winssh.py")


class WindowsDriverTest(unittest.TestCase):
    def setUp(self):
        self.rec = drivers.load(FIXTURE)

    def test_device(self):
        dev = self.rec["device"]
        self.assertEqual((dev["hostname"], dev["vendor"], dev["role"], dev["collector"]),
                         ("srv-win1", "microsoft", "server", "powershell-ssh"))
        self.assertEqual(dev["os_version"], "Microsoft Windows Server 2022 Standard 10.0.20348")

    def test_team_and_members(self):
        team = drivers.interface(self.rec, "Team1")
        self.assertEqual((team["kind"], team["ipv4"], team["speed_mbps"]),
                         ("lag", ["10.0.10.21/24"], 20000))
        for nic in ("Ethernet0", "Ethernet1"):
            self.assertEqual(drivers.interface(self.rec, nic)["parent"], "Team1")
        self.assertFalse(drivers.interface(self.rec, "Ethernet2")["oper_up"])

    def test_loopback_and_apipa_left_out(self):
        addrs = [a for i in self.rec["interfaces"] for a in i["ipv4"] + i["ipv6"]]
        self.assertEqual(addrs, ["10.0.10.21/24", "fe80::a1b2:c3d4:e5f6:1/64"])

    def test_arp_skips_broadcast_multicast_and_unreachable(self):
        self.assertEqual(self.rec["arp"], [{"ip": "10.0.10.1", "mac": "52:54:00:aa:00:01",
                                            "intf": "Team1", "vrf": "default"}])

    def test_powershell_7_names(self):
        core = json.loads(FIXTURE["cli"][windows.CORE_SCRIPT])
        for a in core["adapters"]:
            a["AdminStatus"] = "Up"
        for a in core["addresses"]:
            a["AddressFamily"] = {2: "IPv4", 23: "IPv6"}[a["AddressFamily"]]
        rec = windows.parse_core_json(core)
        self.assertTrue(rec["interfaces"][0]["admin_up"])
        self.assertEqual(rec["interfaces"][0]["ipv4"], ["10.0.10.21/24"])

    def test_server_facet(self):
        server = self.rec["facets"]["server"]
        self.assertEqual(server["domain"], "lab.local")
        self.assertEqual([r["name"] for r in server["roles"]],
                         ["AD-Domain-Services", "DNS", "FileAndStorage-Services"])

    def test_scripts_fit_on_a_cmd_exe_command_line(self):
        # Windows OpenSSH runs the remote command through cmd.exe (8191 chars)
        policy = readonly.policy_for("windows")
        for command in windows.CORE_COMMANDS + drivers.facet_commands("windows"):
            encoded = base64.b64encode(policy.script(command).encode("utf-16-le"))
            self.assertLess(len(encoded) + 60, 8000, command)


class PowerShellSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "server.log"
        self.env = mock.patch.dict(os.environ, {"FAKE_SSH_LOG": str(self.log),
                                                "FAKE_WIN_FIXTURE": str(FIXTURE_PATH)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def received(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def session(self, password="secret", **kw):
        kw.setdefault("policy", readonly.policy_for("windows"))
        return PowerShellSession("192.0.2.10", "LAB\\svc-topo", password, ssh_binary=FAKE,
                                 timeout=10, log=lambda _: None, **kw)

    def test_sends_exactly_the_checked_script(self):
        with self.session() as s:
            out = s.run(windows.CORE_SCRIPT)
        self.assertEqual(json.loads(out)["computer"]["Name"], "SRV-WIN1")
        (got,) = self.received()
        text = readonly.policy_for("windows").script(windows.CORE_SCRIPT)
        self.assertEqual(got["script"], text)
        self.assertEqual(got["remote"], ["powershell", "-NoProfile", "-NonInteractive",
                                         "-EncodedCommand"])
        (entry,) = s.audit
        self.assertEqual(entry["command"], f"{windows.CORE_SCRIPT}#sha256={got['sha256']}")
        self.assertEqual(entry["status"], "ok")
        self.assertTrue(readonly.policy_for("windows").allows(entry["command"]))

    def test_progress_noise_ignored(self):
        with mock.patch.dict(os.environ, {"FAKE_WIN_MODE": "noise"}), self.session() as s:
            self.assertTrue(json.loads(s.run(windows.ROLES_SCRIPT)))

    def test_refused_never_connects(self):
        with self.session() as s:
            for bad in ("powershell:../x.ps1", "Get-NetAdapter | Disable-NetAdapter"):
                with self.subTest(command=bad), self.assertRaises(readonly.CommandRefused):
                    s.run(bad)
        self.assertEqual(self.received(), [])
        self.assertEqual([a["status"] for a in s.audit], ["refused", "refused"])

    def test_without_a_powershell_policy_nothing_is_sent(self):
        for policy in (None, readonly.policy_for("ios")):
            with self.session(policy=policy) as s, self.assertRaises(readonly.CommandRefused):
                s.run(windows.CORE_SCRIPT)
        self.assertEqual(self.received(), [])

    def test_wrong_password(self):
        with self.assertRaises(AuthError) as cm, self.session(password="hunter2") as s:
            s.run(windows.CORE_SCRIPT)
        self.assertIn("Permission denied", str(cm.exception))
        self.assertNotIn("hunter2", str(cm.exception))


class CollectWindowsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        (bin_dir / "ssh").symlink_to(FAKE)
        self.env = mock.patch.dict(os.environ, {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_WIN_FIXTURE": str(FIXTURE_PATH), "NET_USERNAME": "admin",
            "NET_PASSWORD": "wrong", "WINDOWS_USERNAME": "LAB\\svc-topo",
            "WINDOWS_PASSWORD": "secret"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_collect_and_audit(self):
        with redirect_stdout(io.StringIO()) as log, redirect_stderr(io.StringIO()):
            rec = collect.collect("srv-win1", "192.0.2.10", "windows", timeout=10)
        self.assertEqual(sorted(rec["raw"]), sorted(collect.commands("windows")))
        self.assertEqual(rec["audit"]["config_check"], {"status": "unsupported"})
        self.assertEqual(rec["interfaces"], drivers.load(FIXTURE)["interfaces"])
        self.assertEqual(audit.verify(rec), ([], []))
        self.assertIn("as 'LAB\\\\svc-topo'", log.getvalue())  # WINDOWS_* preferred
        self.assertNotIn("secret", json.dumps(rec) + log.getvalue())

    def test_audit_catches_a_changed_script(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rec = collect.collect("srv-win1", "192.0.2.10", "windows", timeout=10)
        entry = rec["audit"]["commands"][0]
        entry["command"] = entry["command"][:-8] + "00000000"
        self.assertTrue(audit.verify(rec)[0])


class ServerPlacementTest(unittest.TestCase):
    """The server, teamed on two NICs, is learned on sw2 Gi1/0/8."""

    def facts(self, tmp):
        facts = tmp / "facts"
        shutil.copytree(support.FIXTURES / "l2", facts)
        shutil.copy(FIXTURE_PATH, facts)
        sw2 = drivers.load(json.loads((facts / "sw2.json").read_text()))
        sw2["interfaces"].append(dict(drivers.interface(sw2, "GigabitEthernet1/0/7"),
                                      name="GigabitEthernet1/0/8", mac="00:a1:b2:c4:01:08"))
        sw2["mac_table"].append({"mac": "00:50:56:01:00:21", "intf": "GigabitEthernet1/0/8",
                                 "vlan": 10})
        (facts / "sw2.json").write_text(json.dumps(sw2))
        return facts

    def test_linked_by_mac_to_the_team(self):
        with tempfile.TemporaryDirectory() as tmp:
            edges, _ = facts_parser.extract_links(facts_parser.load_nodes(self.facts(Path(tmp))))
        (e,) = [e for e in edges if "srv-win1" in (e["source"], e["target"])]
        self.assertEqual((e["source"], e["source_intf"], e["target"], e["target_intf"],
                          e["evidence"], e["target_ip"]),
                         ("sw2", "GigabitEthernet1/0/8", "srv-win1", "Team1",
                          ["mac", "subnet"], "10.0.10.21/24"))

    def test_roles_badge_on_the_diagram(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            facts = self.facts(tmp)
            (tmp / "inventory.yml").write_text("all:\n  hosts:\n" + "".join(
                f"    {n}: {{}}\n" for n in ("rtr1", "sw1", "sw2", "srv1", "srv2", "srv-win1")))
            proc = subprocess.run(
                [sys.executable, str(support.SCRIPTS / "main.py"), str(facts), str(tmp),
                 f"--inventory={tmp / 'inventory.yml'}", f"--template={tmp / 'template.drawio'}"],
                capture_output=True, text=True, cwd=tmp)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            root = ET.parse(tmp / "topology.drawio").getroot().find(".//root")
            badge = root.find("object[@id='roles-srv-win1']")
            self.assertEqual(badge.get("label"), "AD DS · DNS")
            self.assertIn("domain lab.local", badge.get("tooltip"))
            self.assertIn("File and Storage Services", badge.get("tooltip"))
            self.assertEqual(badge.find("mxCell").get("parent"), "layer-roles")

    def test_badge_labels(self):
        topo = mock.Mock(hosts={"a": {}, "b": {}}, nodes={"a": None, "b": None})
        facets = {"a": {"roles": [{"name": "Web-Server", "display_name": "Web Server (IIS)"}]},
                  "b": {"roles": [{"name": "Fax", "display_name": "Fax Server"}]}}
        topo.facet = lambda host, _: facets[host]
        self.assertEqual([(h, t) for h, t, _ in ROLES.collect(topo)],
                         [("a", "IIS"), ("b", "roles")])


if __name__ == "__main__":
    unittest.main()
