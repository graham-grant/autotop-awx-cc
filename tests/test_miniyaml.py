import unittest

import support  # noqa: F401
import miniyaml


class MiniYamlTest(unittest.TestCase):
    def test_project_inventory(self):
        tree = miniyaml.safe_load((support.ROOT / "inventory.yml").read_text())
        hosts = tree["all"]["children"]["network_devices"]["hosts"]
        self.assertEqual(hosts["9200-1"]["ansible_host"], "192.168.10.241")
        self.assertEqual(hosts["9200-1"]["icon"], "C9200-24P Front")
        self.assertEqual(tree["all"]["children"]["artifact_server"]["vars"]["ansible_connection"], "ssh")

    def test_scalars_and_flow(self):
        doc = miniyaml.safe_load("""
---
a: {x: 400, y: 100.5, side: bottom}
b: [1, 'two', "th\\"ree", {}]
c: ~
d: yes
e: 'it''s'   # trailing comment
f: "#not a comment"
g: 10.0.0.1
h: http://host:80/path
""")
        self.assertEqual(doc["a"], {"x": 400, "y": 100.5, "side": "bottom"})
        self.assertEqual(doc["b"], [1, "two", 'th"ree', {}])
        self.assertIsNone(doc["c"])
        self.assertIs(doc["d"], True)
        self.assertEqual(doc["e"], "it's")
        self.assertEqual(doc["f"], "#not a comment")
        self.assertEqual(doc["g"], "10.0.0.1")
        self.assertEqual(doc["h"], "http://host:80/path")

    def test_sequences(self):
        doc = miniyaml.safe_load("""
list:
- 1
- name: x
  vals:
    - a
    - b
- - nested
  - n2
same_indent:
- q
""")
        self.assertEqual(doc["list"], [1, {"name": "x", "vals": ["a", "b"]}, ["nested", "n2"]])
        self.assertEqual(doc["same_indent"], ["q"])

    def test_block_scalars(self):
        doc = miniyaml.safe_load("""
lit: |
  one # kept
  two
fold: >-
  a
  b

  c
next: 1
""")
        self.assertEqual(doc["lit"], "one # kept\ntwo\n")
        self.assertEqual(doc["fold"], "a b\nc")
        self.assertEqual(doc["next"], 1)

    def test_playbook_parses(self):
        plays = miniyaml.safe_load((support.ROOT / "playbooks" / "gather_topology.yml").read_text())
        self.assertIsInstance(plays, list)
        self.assertTrue(all("hosts" in p for p in plays))

    def test_rejects_unsupported(self):
        for text in ("a: &anchor 1\nb: *anchor\n", "a: !!str 1\n", "a: 1\n---\nb: 2\n"):
            with self.assertRaises(miniyaml.YamlError):
                miniyaml.safe_load(text)


if __name__ == "__main__":
    unittest.main()
