"""The dummy devices (dummy/bin/ssh, dummy/inventory) used for a test
run of collect_facts.yml: a whole collection through the real collector
and SSH session, the device's own log equal to the audit, the pager, and
the configuration-change and confirmation safeguards firing."""
import contextlib
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
import miniyaml
import readonly
from sshcli import CliSession

DUMMY = support.ROOT / "dummy"
SSH = str(DUMMY / "bin" / "ssh")
INVENTORY = miniyaml.safe_load((DUMMY / "inventory").read_text())
HOSTS = INVENTORY["all"]["children"]["network_devices"]["hosts"]
PASSWORD = "dummy-secret-1"


def run_collect(name, mode="", device=None):
    """Collect one dummy host as the playbook would; return (record or
    the exception, the lines the dummy device received)."""
    host = HOSTS.get(name) or HOSTS["sw1"]
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp, "device.log")
        env = {"DUMMY_DEVICE": str(support.ROOT / (device or host["dummy_device"])),
               "DUMMY_LOG": str(log), "DUMMY_MODE": mode,
               "NET_USERNAME": "dummy", "NET_PASSWORD": PASSWORD}
        real = CliSession.__init__

        def fake_init(self, *a, **kw):
            real(self, *a, ssh_binary=SSH, **kw)
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(CliSession, "__init__", fake_init), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                result = collect.collect(name, host["ansible_host"], host["ansible_network_os"],
                                         timeout=10, command_timeout=10)
            except Exception as e:  # noqa: BLE001 (the safeguard tests expect one)
                result = e
        received = log.read_text().splitlines() if log.exists() else []
    return result, received


class DummyInventoryTest(unittest.TestCase):
    def test_every_dummy_host_is_ios_with_a_device_file(self):
        self.assertTrue(HOSTS)
        for name, host in HOSTS.items():
            with self.subTest(name):
                self.assertEqual(drivers.platform_key(host["ansible_network_os"]), "ios")
                self.assertTrue((support.ROOT / host["dummy_device"]).is_file())

    def test_dummy_ssh_is_executable(self):
        self.assertTrue(os.access(SSH, os.X_OK))


class DummyCollectionTest(unittest.TestCase):
    def test_every_dummy_host_collects_and_passes_the_check(self):
        for name in HOSTS:
            with self.subTest(name):
                record, received = run_collect(name)
                self.assertIsInstance(record, dict)
                problems, _ = check_facts.check_record(record, name=name)
                self.assertEqual(problems, [])
                # the device's own record is exactly the audit, and the plan
                sent = [e["command"] for e in record["audit"]["commands"]]
                self.assertEqual(received, sent)
                planned = [c for _, c in collect.plan("ios")]
                self.assertEqual(received, planned + ["exit"])
                self.assertNotIn(PASSWORD, "\n".join(received))
                expected = drivers.reparse(json.loads(
                    (support.ROOT / HOSTS[name]["dummy_device"]).read_text()))
                for key in ("interfaces", "neighbors", "arp", "mac_table", "facets"):
                    self.assertEqual(record[key], expected[key], key)

    def test_pager_is_answered_before_paging_is_turned_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"DUMMY_DEVICE": str(support.ROOT / "tests/fixtures/facts/hq.json"),
                   "DUMMY_LOG": str(Path(tmp, "log"))}
            with mock.patch.dict(os.environ, env), \
                    contextlib.redirect_stdout(io.StringIO()):
                with CliSession("hq", "dummy", PASSWORD, ssh_binary=SSH, timeout=10,
                                command_timeout=10, policy=readonly.policy_for("ios")) as s:
                    output = s.run("show crypto session detail")
        expected = json.loads((support.FACTS / "hq.json").read_text())["cli"]
        self.assertNotIn("More", output)
        self.assertEqual(len(output.strip().splitlines()),
                         len(expected["show crypto session detail"].strip().splitlines()))

    def test_configuration_change_fails_the_host(self):
        record, _ = run_collect("sw1", mode="configchange")
        self.assertEqual(record["audit"]["config_check"]["status"], "changed")
        problems, _ = check_facts.check_record(record, name="sw1")
        self.assertIn("$.audit.config_check: configuration changed during collection", problems)

    def test_confirmation_is_cancelled_not_answered(self):
        error, received = run_collect("sw1", mode="confirm")
        self.assertIsInstance(error, readonly.ReadOnlyViolation)
        self.assertIn("ctrl-c", received)
        self.assertNotIn("CONFIRMED", received)
        self.assertEqual(received[-1], "exit")


if __name__ == "__main__":
    unittest.main()
