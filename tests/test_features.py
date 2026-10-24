import unittest
import xml.etree.ElementTree as ET

import support  # noqa: F401
import features
from features import Feature
from test_ipsec import fixture_topology


class Exploding(Feature):
    name = "exploding"
    label = "Broken"

    def collect(self, topo):
        topo.hosts.clear()          # mutation must not leak to other features
        topo.edges.clear()
        raise RuntimeError("boom")

    def render(self, plan, layer):
        pass


class HalfDone(Feature):
    """Adds a cell, then fails: none of its cells may reach the diagram."""
    name = "halfdone"

    def collect(self, topo):
        return None

    def render(self, plan, layer):
        layer.edge("x", "hq", "core1", "dashed=1;")
        raise ValueError("late failure")


class Counter(Feature):
    name = "counter"
    label = "Counter"

    def collect(self, topo):
        return len(topo.hosts), len(topo.edges)

    def render(self, plan, layer):
        self.seen = plan
        layer.edge("e", "hq", "core1", "", points=[(1, 2)])


def empty_root():
    root = ET.Element("root")
    ET.SubElement(root, "mxCell", id="0")
    ET.SubElement(root, "mxCell", id="1", parent="0")
    return root


class FeatureIsolationTest(unittest.TestCase):
    def test_failures_are_contained(self):
        topo = fixture_topology()
        root = empty_root()
        counter = Counter()
        results = features.run_features(root, topo, [Exploding(), HalfDone(), counter])
        self.assertTrue(results["exploding"].startswith("failed"))
        self.assertTrue(results["halfdone"].startswith("failed"))
        self.assertEqual(results["counter"], "ok")
        self.assertEqual(counter.seen, (6, 5))   # untouched by Exploding
        ids = [c.get("id") for c in root]
        self.assertEqual(ids, ["0", "1", "layer-counter", "counter-e"])
        self.assertEqual(len(topo.hosts), 6)     # the shared original too

    def test_layer_cells_parented_to_own_layer(self):
        root = empty_root()
        features.run_features(root, fixture_topology(), [Counter()])
        layer = root.find("mxCell[@id='layer-counter']")
        self.assertEqual(layer.get("parent"), "0")
        self.assertEqual(root.find("mxCell[@id='counter-e']").get("parent"), "layer-counter")

    def test_duplicate_cell_ids_rejected(self):
        layer = features.Layer(Counter())
        layer.edge("a", "hq", "core1", "")
        with self.assertRaises(ValueError):
            layer.edge("a", "hq", "core1", "")

    def test_discovery_and_commands(self):
        self.assertIn("ipsec", features.discover())
        self.assertIn("show crypto session detail", features.commands_for("ios"))
        self.assertEqual(features.commands_for("junos"), [])

    def test_settings_override(self):
        import config
        old = config.FEATURE_SETTINGS
        try:
            config.FEATURE_SETTINGS = {"ipsec": {"up_color": "#123456"}}
            from features.ipsec import IpsecFeature
            f = IpsecFeature()
            self.assertEqual(f.settings["up_color"], "#123456")
            self.assertEqual(f.settings["down_color"], IpsecFeature.SETTINGS["down_color"])
        finally:
            config.FEATURE_SETTINGS = old


if __name__ == "__main__":
    unittest.main()
