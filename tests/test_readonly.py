"""The read-only guarantee: collection never sends a device anything that
could change it. Covers the policy itself, an inventory of every command
any driver or feature can emit, and the enforcement in a live session
against tests/fake_ssh.py (which logs every line it receives)."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import support
import audit
import collect
import features
import drivers
import readonly
from sshcli import CliError, CliSession

FAKE_SSH = str(support.ROOT / "tests" / "fake_ssh.py")

IOS_ALLOWED = [
    "show version",
    "show interfaces",
    "show ip interface",
    "show crypto session detail",
    "show running-config | include configuration change",
    "show ip route vrf MGMT",
    "show interfaces GigabitEthernet1/0/1",
    "show mac address-table | exclude CPU",
    "show lldp neighbors detail | section Local Intf",
    "show logging | count CONFIG_I",
    "terminal length 0",
    "terminal width 512",
    "terminal datadump",
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
    # pipes that write files to the device
    "show running-config | redirect flash:x.txt", "show running-config | tee flash:x",
    "show running-config | append flash:x", "show version | include a | redirect flash:y",
    # smuggling a second line, help, abbreviations, odd spacing
    "show version\nconfigure terminal", "show version\rreload", "show version?",
    "show run\t", "sh run", "show", "", "  show version", "show  version",
    "show version ", "terminal length 24 ; reload", "terminal monitor",
    "terminal length 0\nreload",
    # PAN-OS and Windows verbs, should someone send them to IOS
    "set deviceconfig system hostname x", "commit", "request restart system",
    "Set-NetAdapter -Name x", "Restart-Computer", "Remove-Item C:\\x",
]


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.ios = readonly.policy_for("ios")

    def test_allowed(self):
        for cmd in IOS_ALLOWED:
            with self.subTest(cmd=cmd):
                self.assertEqual(self.ios.check(cmd), cmd)

    def test_refused(self):
        for cmd in IOS_REFUSED:
            with self.subTest(cmd=cmd):
                with self.assertRaises(readonly.CommandRefused):
                    self.ios.check(cmd)

    def test_unknown_platform_has_no_policy(self):
        for platform in ("nxos", "eos", "", None):
            with self.subTest(platform=platform), self.assertRaises(readonly.CommandRefused):
                readonly.policy_for(platform)

    def test_plan_refuses_unknown_platform_before_connecting(self):
        with self.assertRaises(readonly.CommandRefused):
            collect.plan("junos")


class CommandInventoryTest(unittest.TestCase):
    """Every command any driver or feature can emit - enabled or not - must
    pass its platform's policy. Adding a command that doesn't fails here."""

    def test_every_driver_has_a_policy(self):
        for platform in drivers.platforms():
            with self.subTest(platform=platform):
                readonly.policy_for(platform)

    def test_every_driver_command_is_read_only(self):
        for platform in drivers.platforms():
            policy = readonly.policy_for(platform)
            cmds = (drivers.paging_commands(platform) + drivers.core_commands(platform)
                    + [drivers.config_check_command(platform)]
                    + drivers.facet_commands(platform))  # every facet, enabled or not
            for cmd in filter(None, cmds):
                with self.subTest(platform=platform, cmd=cmd):
                    policy.check(cmd)

    def test_every_feature_facet_command_is_read_only(self):
        for name, feature in features.discover().items():
            for platform in drivers.platforms():
                policy = readonly.policy_for(platform)
                for cmd in drivers.facet_commands(platform, [feature.facet]):
                    with self.subTest(feature=name, platform=platform, cmd=cmd):
                        policy.check(cmd)

    def test_default_paging_commands_are_read_only(self):
        for cmd in drivers._DEFAULT_PAGING:
            readonly.policy_for("ios").check(cmd)

    def test_plan_for_every_platform(self):
        for platform in drivers.platforms():
            steps = collect.plan(platform)
            self.assertTrue(steps)


class SessionEnforcementTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "device.log"
        self.env = mock.patch.dict(os.environ, {"FAKE_SSH_MODE": "plain",
                                                "FAKE_SSH_LOG": str(self.log)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def device_log(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def session(self, **kw):
        kw.setdefault("policy", readonly.policy_for("ios"))
        return CliSession("sw", "admin", "secret", ssh_binary=FAKE_SSH,
                          timeout=10, command_timeout=10, **kw)

    def test_refused_command_never_reaches_the_device(self):
        with self.session() as s:
            with self.assertRaises(readonly.CommandRefused):
                s.run("configure terminal")
            self.assertEqual(s.run("show version"), "SW version    3.0.0.44\n")
            self.assertEqual(s.audit[0]["status"], "refused")
        self.assertNotIn("configure terminal", self.device_log())
        self.assertIn("show version", self.device_log())

    def test_no_policy_sends_nothing(self):
        with self.session(policy=None) as s:
            with self.assertRaises(readonly.CommandRefused):
                s.run("show version")
        self.assertNotIn("show version", self.device_log())

    def test_confirmation_prompt_is_cancelled_not_answered(self):
        # "show confirm-test" stands in for a command that unexpectedly asks
        # to confirm something; the guard must cancel it, not press enter
        with self.session() as s:
            with self.assertRaises(readonly.UnexpectedPrompt):
                s.run("show confirm-test")
            self.assertEqual(s.audit[-1]["status"], "cancelled")
            with self.assertRaisesRegex(CliError, "abandoned"):
                s.run("show version")
        log = self.device_log()
        self.assertIn("ctrl-c", log)
        self.assertNotIn("CONFIRMED", log)
        self.assertNotIn("show version", log)

    def test_timeout_cancels_with_ctrl_c_before_continuing(self):
        with self.session() as s:
            with self.assertRaisesRegex(CliError, "timed out"):
                s.run("show hang", timeout=1)
            self.assertEqual(s.run("show version"), "SW version    3.0.0.44\n")
        log = self.device_log()
        self.assertEqual(log[log.index("show hang") + 1], "ctrl-c")


class CollectAuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "device.log"

    def tearDown(self):
        self.tmp.cleanup()

    def collect(self, mode="plain", cmds=("show version", "show interfaces status")):
        env = {"FAKE_SSH_MODE": mode, "FAKE_SSH_LOG": str(self.log),
               "NET_USERNAME": "admin", "NET_PASSWORD": "secret"}
        real = CliSession.__init__

        def fake_init(self, *a, **kw):
            real(self, *a, ssh_binary=FAKE_SSH, **kw)
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(CliSession, "__init__", fake_init), \
                mock.patch.object(collect, "commands", lambda p: list(cmds)), \
                redirect_stdout(io.StringIO()) as out:
            rec = collect.collect("AT-8K-1", "sw", "ios", timeout=10, command_timeout=10)
        return rec, out.getvalue()

    def test_audit_matches_what_the_device_received(self):
        rec, out = self.collect()
        sent = [e["command"] for e in rec["audit"]["commands"]]
        self.assertEqual(sent[-1], "exit")
        received = [l for l in self.log.read_text().splitlines() if l != "exit"]
        self.assertEqual(sent[:-1], received)
        self.assertEqual(sent[:-1], [
            "terminal length 0",
            "show running-config | include configuration change",
            "show version", "show interfaces status",
            "show running-config | include configuration change"])
        self.assertTrue(all(readonly.policy_for("ios").allows(c) for c in sent[:-1]))
        self.assertIn("AT-8K-1:     sent  show version", out)
        self.assertEqual(rec["audit"]["config_check"]["status"], "unchanged")
        self.assertEqual(audit.verify(rec), ([], []))

    def test_config_change_fails_loudly(self):
        rec, _ = self.collect(mode="configchange")
        check = rec["audit"]["config_check"]
        self.assertEqual(check["status"], "changed")
        self.assertNotEqual(check["before"], check["after"])
        problems, _ = audit.verify(rec)
        self.assertTrue(any("configuration changed" in p for p in problems))

    def test_main_exits_3_on_config_change(self):
        out_file = Path(self.tmp.name) / "r1.json"
        env = {"FAKE_SSH_MODE": "configchange", "FAKE_SSH_LOG": str(self.log),
               "NET_USERNAME": "admin", "NET_PASSWORD": "secret"}
        real = CliSession.__init__

        def fake_init(self, *a, **kw):
            real(self, *a, ssh_binary=FAKE_SSH, **kw)
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(CliSession, "__init__", fake_init), \
                mock.patch.object(collect, "commands", lambda p: ["show version"]), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit) as cm:
            collect.main(["--name", "r1", "--host", "sw", "--platform", "ios",
                          "--out", str(out_file), "--timeout", "10"])
        self.assertEqual(cm.exception.code, 3)
        self.assertIn("CONFIGURATION CHANGED", err.getvalue())
        self.assertTrue(out_file.exists())

    def test_refused_feature_command_stops_before_connecting(self):
        with self.assertRaises(readonly.CommandRefused):
            self.collect(cmds=("show version", "clear counters"))
        self.assertFalse(self.log.exists())


class AuditScriptTest(unittest.TestCase):
    def record(self, **audit_overrides):
        a = {"policy": "ios", "commands": [
                {"command": "terminal length 0", "kind": "session", "status": "ok"},
                {"command": "show version", "kind": "collect", "status": "ok"},
                {"command": "exit", "kind": "session", "status": "sent"}],
             "config_check": {"status": "unchanged"}}
        a.update(audit_overrides)
        return {"inventory_name": "r1", "platform": "ios", "cli": {}, "audit": a}

    def test_clean_record_passes(self):
        self.assertEqual(audit.verify(self.record()), ([], []))

    def test_disallowed_command_fails(self):
        rec = self.record()
        rec["audit"]["commands"].append({"command": "write memory", "kind": "collect",
                                         "status": "ok"})
        problems, _ = audit.verify(rec)
        self.assertTrue(problems)

    def test_missing_audit_fails(self):
        problems, _ = audit.verify({"inventory_name": "r1", "platform": "ios", "cli": {}})
        self.assertIn("no audit record", problems[0])

    def test_unavailable_check_warns(self):
        problems, warnings = audit.verify(self.record(config_check={"status": "unavailable"}))
        self.assertEqual(problems, [])
        self.assertTrue(warnings)

    def test_script_exit_codes(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "r1.json").write_text(json.dumps(self.record()))
            with redirect_stdout(io.StringIO()):
                audit.main([d])
            Path(d, "r2.json").write_text(json.dumps(
                self.record(config_check={"status": "changed"})))
            with redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as cm:
                audit.main([d])
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("FAIL  r1", out.getvalue().replace("FAIL  r2", "FAIL  r1"))


if __name__ == "__main__":
    unittest.main()
