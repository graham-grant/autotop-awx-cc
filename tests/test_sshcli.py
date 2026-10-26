import os
import unittest
from unittest import mock

import support
import readonly
from sshcli import AuthError, CliSession

FAKE_SSH = str(support.ROOT / "tests" / "fake_ssh.py")


def session(**kw):
    kw.setdefault("policy", readonly.policy_for("ios"))
    return CliSession("sw", "admin", kw.pop("password", "secret"), ssh_binary=FAKE_SSH,
                      timeout=10, command_timeout=10, **kw)


class CliSessionTest(unittest.TestCase):
    def test_inband_login_and_pager(self):
        """The AT-8000 case: SSH password, then User Name:/Password: again."""
        with mock.patch.dict(os.environ, {"FAKE_SSH_MODE": "inband"}), session() as s:
            self.assertEqual(s.prompt, b"AT-8K-1#")
            out = s.run("show interfaces status")
            self.assertEqual(out.splitlines(), [f"g{i}  1G-Copper  Full  1000  Up" for i in range(1, 13)])
            self.assertEqual(s.run("show version"), "SW version    3.0.0.44\n")

    def test_pager_answered_once_when_its_erase_arrives_alone(self):
        """The pager erases itself with "\r    \r"; if that arrives in a
        read of its own, the erased prompt mustn't be answered again (the
        extra space would be typed in front of the next command)."""
        env = {"FAKE_SSH_MODE": "inband", "FAKE_SSH_SLOW_ERASE": "1"}
        with mock.patch.dict(os.environ, env), session() as s:
            self.assertEqual(len(s.run("show interfaces status").splitlines()), 12)
            self.assertEqual(s.run("show version"), "SW version    3.0.0.44\n")

    def test_paging_disabled(self):
        with mock.patch.dict(os.environ, {"FAKE_SSH_MODE": "inband"}), session() as s:
            s.disable_paging(["terminal length 0", "terminal datadump"])
            self.assertEqual(len(s.run("show interfaces status").splitlines()), 12)

    def test_plain_login(self):
        with mock.patch.dict(os.environ, {"FAKE_SSH_MODE": "plain"}), session() as s:
            self.assertEqual(s.run("show version"), "SW version    3.0.0.44\n")

    def test_wrong_password(self):
        with mock.patch.dict(os.environ, {"FAKE_SSH_MODE": "plain"}):
            with self.assertRaises(AuthError) as cm:
                session(password="nope").open()
            self.assertNotIn("nope", str(cm.exception))

    def test_inband_rejected(self):
        with mock.patch.dict(os.environ, {"FAKE_SSH_MODE": "wrongpass"}):
            with self.assertRaises(AuthError):
                session().open()


NO_CREDS = {v: "" for v in ("NET_USERNAME", "NET_PASSWORD", "ANSIBLE_NET_USERNAME",
                             "ANSIBLE_NET_PASSWORD", "NAPALM_USERNAME", "NAPALM_PASSWORD")}


class CollectTest(unittest.TestCase):
    def test_no_credentials_is_a_clear_error(self):
        import collect
        from sshcli import CliError
        with mock.patch.dict(os.environ, NO_CREDS):
            with self.assertRaisesRegex(CliError, "Network' credential"):
                collect.collect("r1", "sw", "ios")

    def test_collect_uses_awx_network_credential_env(self):
        import collect
        env = {**NO_CREDS, "FAKE_SSH_MODE": "inband",
               "ANSIBLE_NET_USERNAME": "admin", "ANSIBLE_NET_PASSWORD": "secret"}
        real = CliSession.__init__

        def fake_init(self, *a, **kw):
            real(self, *a, ssh_binary=FAKE_SSH, **kw)
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(CliSession, "__init__", fake_init), \
                mock.patch.object(collect, "commands",
                                  lambda p: ["show version", "show bogus", "show interfaces status"]):
            rec = collect.collect("AT-8K-1", "sw", "ios", timeout=10, command_timeout=10)
        self.assertEqual(rec["schema"], 2)
        self.assertEqual(rec["device"]["name"], "AT-8K-1")
        self.assertIsNotNone(rec["device"]["collected_at"])
        self.assertEqual(list(rec["raw"]), ["show version", "show interfaces status"])
        self.assertEqual(rec["audit"]["config_check"]["status"], "unchanged")


if __name__ == "__main__":
    unittest.main()
