"""The conformance kits (conformance/) used to check the production rebuild:
each committed kit must match what the reference code does now, the
reference code must pass it, and the runner and the facts checker must
catch a difference."""
import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import support

sys.path.insert(0, str(support.ROOT / "conformance"))

import build_kit  # noqa: E402
import check_facts  # noqa: E402
import run_kit  # noqa: E402

KITS = {stage: support.ROOT / "conformance" / f"stage{stage}" / "kit.json"
        for stage in build_kit.STAGES}


def run_quiet(path):
    lines = []
    with redirect_stdout(io.StringIO()):
        ok = run_kit.run(path, out=lines.append)
    return ok, lines


class KitTest(unittest.TestCase):
    def test_committed_kits_match_the_reference(self):
        # Fails after any change to behaviour a kit covers: rebuild with
        # python3 conformance/build_kit.py <stage>, and hand the new kit to
        # the production rebuild with the change.
        for stage, path in KITS.items():
            with self.subTest(stage=stage), redirect_stdout(io.StringIO()):
                built = build_kit.dumps(build_kit.build(stage))
            self.assertEqual(built, path.read_text(encoding="utf-8"),
                             f"conformance/stage{stage}/kit.json is out of date: "
                             f"run python3 conformance/build_kit.py {stage}")

    def test_reference_passes_each_stage(self):
        for stage, path in KITS.items():
            with self.subTest(stage=stage), build_kit.stage_view(stage):
                ok, lines = run_quiet(path)
                self.assertTrue(ok, "\n".join(lines))
                self.assertTrue(lines[-1].startswith("RESULT PASS"))

    def test_stage_1_refuses_platforms_it_does_not_have(self):
        # The full code has PAN-OS and Windows; stage 1 must not, and the
        # kit must notice the difference
        ok, lines = run_quiet(KITS[1])
        self.assertFalse(ok)
        self.assertTrue(any("policy_for('panos')" in line for line in lines))

    def test_a_changed_expectation_fails(self):
        kit = json.loads(KITS[1].read_text(encoding="utf-8"))
        for v in kit["vectors"]:
            if v["kind"] == "policy" and v["command"] == "configure terminal":
                v["expect"]["allows"] = True
        with tempfile.TemporaryDirectory() as d, build_kit.stage_view(1):
            path = Path(d, "kit.json")
            path.write_text(json.dumps(kit))
            ok, lines = run_quiet(path)
        self.assertFalse(ok)
        self.assertTrue(any(line.startswith("FAIL ios#") and "configure terminal" in line
                            for line in lines))

    def test_runner_that_does_not_compare_fails_the_self_test(self):
        real = run_kit.first_difference
        run_kit.first_difference = lambda e, a, path="$": None
        try:
            with build_kit.stage_view(1):
                ok, lines = run_quiet(KITS[1])
        finally:
            run_kit.first_difference = real
        self.assertFalse(ok)
        self.assertTrue(any("deliberate mismatch" in line for line in lines))

    def test_comparison_is_type_and_order_strict(self):
        diff = run_kit.first_difference
        self.assertIsNone(diff({"a": [1, 2.5, None]}, {"a": [1, 2.5, None]}))
        self.assertIsNotNone(diff(1, 1.0))
        self.assertIsNotNone(diff(1, True))
        self.assertIsNotNone(diff(None, ""))
        self.assertIsNotNone(diff({"a": 1, "b": 2}, {"b": 2, "a": 1}))
        self.assertIsNotNone(diff([1], [1, 2]))


class CheckFactsTest(unittest.TestCase):
    def setUp(self):
        self.kit = json.loads(KITS[1].read_text(encoding="utf-8"))
        good = next(v for v in self.kit["vectors"] if v["id"] == "check_record[good]")
        self.good = good["args"][0]

    def run_main(self, records):
        with tempfile.TemporaryDirectory() as d:
            for name, rec in records.items():
                Path(d, f"{name}.json").write_text(json.dumps(rec, indent=2))
            out = io.StringIO()
            code = 0
            with redirect_stdout(out):
                try:
                    check_facts.main([str(KITS[1]), d])
                except SystemExit as e:
                    code = e.code
            return code, out.getvalue()

    def test_good_file_passes_and_fingerprint_has_no_data(self):
        code, out = self.run_main({"branch1": self.good})
        self.assertEqual(code, 0, out)
        self.assertIn("CHECK PASS 1 file(s)", out)
        for secret in ("branch1", "10.0.0.10", "52:54:00", "Tunnel1"):
            self.assertNotIn(secret, out)

    def test_bad_file_fails_without_naming_it(self):
        bad = copy.deepcopy(self.good)
        bad["interfaces"][0]["mtu"] = "1500"
        code, out = self.run_main({"branch1": bad})
        self.assertEqual(code, 1)
        self.assertIn("$.interfaces[0].mtu: type str, expected int|null", out)
        self.assertNotIn("branch1", out)

    def test_compare(self):
        later = copy.deepcopy(self.good)
        later["device"]["collected_at"] = "2027-01-01T00:00:00+00:00"
        self.assertIsNone(check_facts.compare(self.good, later))
        later["interfaces"][0]["mtu"] = 9000
        self.assertEqual(check_facts.compare(self.good, later)[0], "$.interfaces[0].mtu")


if __name__ == "__main__":
    unittest.main()
