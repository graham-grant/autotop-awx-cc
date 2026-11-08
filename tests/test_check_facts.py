"""check_facts.py: the stage 1 facts checker, on records the drivers build
from fixture output, on a whole collection through the real SSH session
against the fake device, and on one broken record per rule."""
import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401 (puts scripts/ on sys.path)
import check_facts
import collect
import drivers
from sshcli import CliSession

FAKE_SSH = str(support.ROOT / "tests" / "fake_ssh.py")
SW1 = support.FIXTURES / "l2" / "sw1.json"
TIME = "2026-11-08T07:00:00+00:00"


def fixture_records():
    for path in sorted(support.FIXTURES.glob("*/*.json")):
        record = drivers.reparse(json.loads(path.read_text()))
        if record["device"]["collector"] != "fixture":  # hand-written, not a driver's
            yield path.stem, record


def with_audit(record):
    """Give a fixture record the audit collect.py would have written."""
    record = copy.deepcopy(record)
    record["device"]["collected_at"] = TIME
    platform = record["device"]["platform"]
    lines = collect.plan(platform)
    record["raw"] = {c: record["raw"][c] for k, c in lines
                     if k == "collect" and c in record["raw"]}
    if drivers.transport(platform) == "ssh-cli":
        commands = [{"time": TIME, "command": c, "kind": k, "status": "ok"} for k, c in lines]
        commands.append({"time": TIME, "command": "exit", "kind": "session", "status": "sent"})
        check = {"status": "unchanged", "command": lines[1][1], "before": "! x", "after": "! x"}
    else:
        commands, check = [], {"status": "unsupported"}
    record["audit"] = {"policy": platform, "commands": commands, "config_check": check}
    return record


def collect_sw1():
    env = {"FAKE_SSH_MODE": "plain", "FAKE_SSH_RAW": str(SW1),
           "NET_USERNAME": "admin", "NET_PASSWORD": "secret"}
    real = CliSession.__init__

    def fake_init(self, *a, **kw):
        real(self, *a, ssh_binary=FAKE_SSH, **kw)
    with mock.patch.dict(os.environ, env), \
            mock.patch.object(CliSession, "__init__", fake_init), \
            contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        return collect.collect("sw1", "sw", "ios", timeout=10, command_timeout=10)


class FixtureRecordsTest(unittest.TestCase):
    def test_every_driver_record_passes(self):
        for name, record in fixture_records():
            with self.subTest(name):
                problems, _ = check_facts.check_record(with_audit(record), name=name)
                self.assertEqual(problems, [])


class CollectedRecordTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.record = collect_sw1()

    def test_collected_over_ssh_passes_and_matches_the_driver(self):
        problems, warnings = check_facts.check_record(self.record, name="sw1")
        self.assertEqual(problems, [])
        expected = drivers.reparse(json.loads(SW1.read_text()))
        for key in ("interfaces", "neighbors", "arp", "mac_table", "facets"):
            self.assertEqual(self.record[key], expected[key], key)
        # the fixture has no ARP or crypto output, so those are skipped
        self.assertTrue(any("show ip arp" in w for w in warnings))

    def test_main_prints_check_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "sw1.json").write_text(json.dumps(self.record))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                check_facts.main([tmp])
        self.assertIn("C9300-24T", out.getvalue())
        self.assertIn("CHECK PASS 1 file(s), 0 with problems", out.getvalue())

    def broken(self, change, name="sw1"):
        record = copy.deepcopy(self.record)
        change(record)
        return check_facts.check_record(record, name=name)

    def assertProblem(self, change, text, name="sw1"):
        problems, _ = self.broken(change, name)
        self.assertTrue(any(text in p for p in problems), problems)

    def test_each_rule_reports_a_problem(self):
        def setitem(path, value):
            def change(r):
                target = r
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
            return change
        cases = [
            (setitem(["schema"], 1), "$.schema"),
            (setitem(["interfaces", 0, "mac"], "AA-BB-CC-DD-EE-FF"), "not a valid mac"),
            (setitem(["interfaces", 0, "mtu"], "1500"), "is str, expected int or null"),
            (setitem(["interfaces", 0, "kind"], "ethernet"), "not one of"),
            (setitem(["interfaces", 0, "ipv4"], ["10.0.0.1"]), "not a valid ipv4_cidr"),
            (setitem(["interfaces", 0, "extra"], 1), "unexpected key 'extra'"),
            (lambda r: r["device"].pop("model"), "missing key 'model'"),
            (setitem(["device", "collected_at"], "2026-11-08T07:00:00"), "iso_time"),
            (setitem(["raw", "show running-config"], "x"), "not in the plan"),
            (lambda r: r["interfaces"].append(dict(r["interfaces"][0])), "not unique"),
            (lambda r: r["audit"]["commands"].insert(
                3, {"time": TIME, "command": "show clock", "kind": "collect", "status": "ok"}),
             "not exactly the plan"),
            (setitem(["audit", "commands", 3, "status"], "refused"), "was refused"),
            (setitem(["audit", "config_check", "status"], "changed"), "configuration changed"),
            (lambda r: r.pop("audit"), "missing key 'audit'"),
            (setitem(["device", "platform"], "junos"), "not one of"),
        ]
        for change, text in cases:
            with self.subTest(text):
                self.assertProblem(change, text)
        self.assertProblem(lambda r: None, "not the file's name", name="sw2")

    def test_warnings(self):
        def no_interfaces(r):
            r["interfaces"] = []
        _, warnings = self.broken(no_interfaces)
        self.assertIn("$.interfaces: empty", warnings)
        self.assertTrue(any("local_intf" in w for w in warnings))
        _, warnings = self.broken(lambda r: r["raw"].update({"show version": " "}))
        self.assertIn("$.raw: empty output for ['show version']", warnings)

        def unreadable_marker(r):
            r["audit"]["config_check"] = {"status": "unavailable", "command": "x",
                                          "before": None, "after": None}
        problems, warnings = self.broken(unreadable_marker)
        self.assertEqual(problems, [])
        self.assertTrue(any("could not be read" in w for w in warnings))

    def test_timeout_that_stopped_the_run_is_a_warning(self):
        def stopped(r):
            del r["audit"]["commands"][5:]
            r["audit"]["commands"][4]["status"] = "timeout"
            r["raw"] = {"show version": r["raw"]["show version"]}
        problems, warnings = self.broken(stopped)
        self.assertEqual(problems, [])
        self.assertTrue(any("stopped after a timeout" in w for w in warnings))

    def test_bad_files_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "bad.json").write_text("{not json")
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
                check_facts.main([str(Path(tmp, "bad.json")), str(Path(tmp, "gone.json"))])
        self.assertIn("FAIL  bad: not valid JSON", out.getvalue())
        self.assertIn("FAIL  gone: no facts file", out.getvalue())
        self.assertIn("CHECK FAIL 2 file(s), 2 with problems", out.getvalue())


class PlaybookTest(unittest.TestCase):
    def test_stage_gate_knows_every_platform_alias(self):
        import miniyaml
        plays = miniyaml.safe_load((support.ROOT / "playbooks" / "collect_facts.yml").read_text())
        collect_play = next(p for p in plays if p["hosts"] == "network_devices")
        aliases = {k: v for k, v in drivers._ALIASES.items() if k != v}
        self.assertEqual(collect_play["vars"]["platform_aliases"], aliases)


if __name__ == "__main__":
    unittest.main()
