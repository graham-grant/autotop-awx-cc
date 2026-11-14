"""Publishing the diagram to a git repository (gather_topology.yml): the
script that answers git's prompts, no credential on a command line in any
playbook, and Visio files that only change when the diagram does (so the
repository's history shows when the topology changed)."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401 (puts scripts/ on sys.path)
import diagram as dg
import miniyaml
import positions
import vsdx

PLAYBOOKS = support.ROOT / "playbooks"
ASKPASS = support.ROOT / "scripts" / "git_askpass.sh"
CREDS = {"BITBUCKET_USERNAME": "svc-autotop", "BITBUCKET_TOKEN": "t0ken with spaces & $ymbols"}
# Ansible puts every value under `environment:` on a command line, so only
# these, which are never secret, may be set there
ENVIRONMENT_ALLOWED = {"PATH", "DUMMY_DEVICE", "DUMMY_LOG", "DUMMY_MODE",
                       "GIT_ASKPASS", "GIT_TERMINAL_PROMPT"}


def plays(name):
    return miniyaml.safe_load((PLAYBOOKS / name).read_text())


def tasks(name):
    for play in plays(name):
        for task in play.get("tasks") or []:
            yield play, task


def one_shape(x=0):
    page = dg.Page("p1", "Page")
    page.add(dg.Shape("sw1", text="sw1", x=x, y=0, w=120, h=60))
    return dg.Diagram([page])


def at_time(stamp):
    """Make vsdx.py believe the time is `stamp`."""
    patch = mock.patch.object(vsdx, "datetime")
    patch.start().now.return_value.strftime.return_value = stamp
    return patch


class AskpassTest(unittest.TestCase):
    def ask(self, prompt):
        return subprocess.run([str(ASKPASS), prompt], env=dict(os.environ, **CREDS),
                              stdout=subprocess.PIPE, universal_newlines=True, check=True).stdout

    def test_is_executable(self):
        self.assertTrue(os.access(str(ASKPASS), os.X_OK))

    def test_answers_the_username_and_the_password_prompt(self):
        self.assertEqual(self.ask("Username for 'https://bitbucket.example.com': "),
                         CREDS["BITBUCKET_USERNAME"] + "\n")
        self.assertEqual(self.ask("Password for 'https://svc-autotop@bitbucket.example.com': "),
                         CREDS["BITBUCKET_TOKEN"] + "\n")

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_git_gets_the_credential_from_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, GIT_ASKPASS=str(ASKPASS), GIT_TERMINAL_PROMPT="0",
                       GIT_CONFIG_NOSYSTEM="1", HOME=tmp, **CREDS)
            out = subprocess.run(["git", "-c", "credential.helper=", "credential", "fill"],
                                 input="protocol=https\nhost=bitbucket.example.com\n\n",
                                 env=env, cwd=tmp, stdout=subprocess.PIPE,
                                 universal_newlines=True, check=True).stdout
        self.assertIn("username=" + CREDS["BITBUCKET_USERNAME"] + "\n", out)
        self.assertIn("password=" + CREDS["BITBUCKET_TOKEN"] + "\n", out)


class PlaybookCredentialTest(unittest.TestCase):
    def test_no_playbook_sets_a_credential_under_environment(self):
        for path in sorted(PLAYBOOKS.glob("*.yml")):
            for play, task in tasks(path.name):
                for scope in (play, task):
                    extra = set(scope.get("environment") or {}) - ENVIRONMENT_ALLOWED
                    self.assertEqual(extra, set(), f"{path.name}: {task.get('name')}")

    def test_the_token_is_only_ever_checked_for_being_set(self):
        # no playbook reads the token's value: git gets it from its own
        # environment, through git_askpass.sh
        for path in sorted(PLAYBOOKS.glob("*.yml")):
            text = path.read_text()
            lookups = re.findall(r"lookup\('env', 'BITBUCKET_TOKEN'\).*", text)
            self.assertEqual(len(lookups), len(re.findall(r"['\"]BITBUCKET_TOKEN['\"]", text)), path.name)
            for found in lookups:
                self.assertEqual(found, "lookup('env', 'BITBUCKET_TOKEN') | length > 0", path.name)

    def test_git_never_keeps_or_reads_a_stored_credential(self):
        for _, task in tasks("gather_topology.yml"):
            command = task.get("command") or ""
            if re.search(r"\b(clone|push)\b", command):
                self.assertIn("-c credential.helper= ", command, task["name"])
                self.assertEqual(task["environment"]["GIT_TERMINAL_PROMPT"], "0")
                self.assertTrue(task["environment"]["GIT_ASKPASS"].endswith("/scripts/git_askpass.sh"))


class PlaybookOrderTest(unittest.TestCase):
    def test_repository_is_checked_before_collection_and_built_after_the_audit(self):
        names = [p.get("import_playbook") or p["name"] for p in plays("gather_topology.yml")]
        self.assertEqual(names, ["Clone the publish repository", "collect_facts.yml",
                                 "Build the diagram (Visio and draw.io)",
                                 "Publish the diagram to the repository"])
        # the imported plays end with the audit and the JSON check
        self.assertEqual(plays("collect_facts.yml")[-1]["name"],
                         "Audit the commands sent and check the JSON")

    def test_only_the_collection_play_runs_on_the_devices(self):
        for name in ("gather_topology.yml", "collect_facts.yml", "preflight.yml"):
            for play in plays(name):
                if "hosts" in play and play["hosts"] != "localhost":
                    self.assertEqual((name, play["name"]),
                                     ("collect_facts.yml", "Collect show-command output, read-only"))


class UnchangedFileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "topology.vsdx"
        patch = at_time("2026-01-01T00:00:00Z")
        self.addCleanup(mock.patch.stopall)
        self.assertNotIn("unchanged", vsdx.write(one_shape(), self.path))
        patch.stop()
        self.first = self.path.read_bytes()

    def test_same_diagram_later_leaves_the_file_alone(self):
        at_time("2026-06-01T00:00:00Z")
        self.assertTrue(vsdx.write(one_shape(), self.path).get("unchanged"))
        self.assertEqual(self.path.read_bytes(), self.first)

    def test_changed_diagram_is_written(self):
        at_time("2026-06-01T00:00:00Z")
        self.assertNotIn("unchanged", vsdx.write(one_shape(x=40), self.path))
        self.assertNotEqual(self.path.read_bytes(), self.first)
        self.assertFalse(vsdx.same_content(self.path, _bytes(self.first)))

    def test_a_file_that_is_not_a_vsdx_is_replaced(self):
        self.path.write_text("not a zip")
        self.assertNotIn("unchanged", vsdx.write(one_shape(), self.path))
        self.assertTrue(vsdx.same_content(self.path, _bytes(self.first)))

    def test_template_with_the_same_nodes_is_left_alone(self):
        template = self.path.with_name("template.vsdx")
        node = dg.Shape("sw1", x=0, y=0, w=120, h=60)
        at_time("2026-01-01T00:00:00Z")
        positions.write_template(template, [node], {})
        first = template.read_bytes()
        mock.patch.stopall()
        at_time("2026-06-01T00:00:00Z")
        positions.write_template(template, [node], {})
        self.assertEqual(template.read_bytes(), first)
        positions.write_template(template, [node], {"sw1": "left"})
        self.assertNotEqual(template.read_bytes(), first)


def _bytes(data):
    import io
    return io.BytesIO(data)


if __name__ == "__main__":
    unittest.main()
