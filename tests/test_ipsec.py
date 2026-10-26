import json
import math
import unittest

import support
import parser as facts_parser
from pathfinder import Node
from features.context import Topology
from features.ipsec import FEATURE as IPSEC
from drivers.ios import parse_crypto_session as parse_ios_crypto_session
from features import Layer

# Same layout as fixtures/template.drawio: hq on top, core1/core2 in the
# middle, three branches along the bottom.
LAYOUT = {
    "hq": (285, 0), "core1": (285, 200), "core2": (685, 200),
    "branch1": (585, 420), "branch2": (885, 420), "branch3": (85, 420),
}


def fixture_topology():
    hosts = facts_parser.load_nodes(support.FACTS)
    edges, _ = facts_parser.extract_links(hosts)
    nodes = [Node(name, x, y, 230, 42) for name, (x, y) in LAYOUT.items()]
    return Topology(hosts, edges, nodes)


class CryptoSessionParserTest(unittest.TestCase):
    def test_up_and_down_sessions(self):
        text = json.loads((support.FACTS / "hq.json").read_text())["cli"]["show crypto session detail"]
        tunnels = parse_ios_crypto_session(text)
        self.assertEqual([t["interface"] for t in tunnels], ["Tunnel1", "Tunnel2", "Tunnel3"])
        self.assertEqual(tunnels[0], {"interface": "Tunnel1", "local_address": "172.16.0.1",
                                      "peer_address": "172.16.0.11", "status": "UP-ACTIVE"})
        # DOWN session has no IKE SA: local address comes from the flow line
        self.assertEqual(tunnels[2]["status"], "DOWN")
        self.assertEqual(tunnels[2]["local_address"], "172.16.0.1")

    def test_empty_output(self):
        self.assertEqual(parse_ios_crypto_session(""), [])
        self.assertEqual(parse_ios_crypto_session("There are no crypto sessions\n"), [])


class IpsecCollectTest(unittest.TestCase):
    def setUp(self):
        self.topo = fixture_topology()
        self.tunnels = IPSEC.collect(self.topo)

    def test_both_ends_merge_into_one_tunnel(self):
        self.assertEqual(len(self.tunnels), 3)
        pairs = {frozenset((t.a, t.b)) for t in self.tunnels}
        self.assertEqual(pairs, {frozenset(("hq", b)) for b in ("branch1", "branch2", "branch3")})

    def test_underlay_path_crosses_every_router(self):
        by_peer = {t.b if t.a == "hq" else t.a: t for t in self.tunnels}
        path = by_peer["branch1"].path
        if path[0] != "hq":
            path = path[::-1]
        self.assertEqual(path, ["hq", "core1", "core2", "branch1"])

    def test_status(self):
        by_peer = {t.b if t.a == "hq" else t.a: t for t in self.tunnels}
        self.assertTrue(by_peer["branch1"].is_up)
        self.assertFalse(by_peer["branch3"].is_up)

    def test_prenormalized_records_and_explicit_path(self):
        topo = fixture_topology()
        for host in topo.hosts.values():
            host["facets"].pop("ipsec", None)
        topo.hosts["hq"]["facets"]["ipsec"] = {"tunnels": [{
            "interface": "Tunnel9", "local_address": "172.16.0.1",
            "peer_address": "172.16.0.12", "status": "up",
            "path": ["hq", "core1", "core2", "branch2"]}]}
        (t,) = IPSEC.collect(topo)
        self.assertEqual(t.path, ["branch2", "core2", "core1", "hq"] if t.a == "branch2"
                         else ["hq", "core1", "core2", "branch2"])

    def test_unknown_peer_skipped(self):
        topo = fixture_topology()
        for host in topo.hosts.values():
            host["facets"].pop("ipsec", None)
        topo.hosts["hq"]["facets"]["ipsec"] = {"tunnels": [{
            "interface": "Tunnel9", "local_address": "172.16.0.1",
            "peer_address": "203.0.113.9", "status": "UP-ACTIVE"}]}
        self.assertEqual(IPSEC.collect(topo), [])


class IpsecGeometryTest(unittest.TestCase):
    def setUp(self):
        self.topo = fixture_topology()
        self.plan = IPSEC.plan(IPSEC.collect(self.topo), self.topo)

    def _hq_lane(self, t, g):
        """(face point, first three approach points) at the hq end."""
        hq = self.topo.nodes["hq"]
        if g.source == "hq":
            fx, fy = g.exit
            pts = g.points[:3]
        else:
            fx, fy = g.entry
            pts = g.points[-3:][::-1]
        return (hq.x + fx * hq.w, hq.y + fy * hq.h), pts

    def test_tunnels_converge_in_parallel_lanes_at_shared_endpoint(self):
        lanes = [self._hq_lane(t, g) for t, g in self.plan]
        xs = sorted(face[0] for face, _ in lanes)
        # distinct, evenly spaced attachment points on the same face
        gaps = [b - a for a, b in zip(xs, xs[1:])]
        self.assertTrue(all(g > 1 for g in gaps))
        self.assertAlmostEqual(gaps[0], gaps[1])
        self.assertEqual(len({round(face[1], 6) for face, _ in lanes}), 1)
        # each lane leaves straight along the face normal (parallel run)
        for face, pts in lanes:
            for p in pts:
                self.assertAlmostEqual(p[0], face[0])
            self.assertTrue(all(p[1] > face[1] for p in pts))  # hq's bottom face

    def test_transit_waypoint_over_each_router(self):
        for t, g in self.plan:
            path = t.path if g.source == t.path[0] else t.path[::-1]
            for hop in path[1:-1]:
                n = self.topo.nodes[hop]
                self.assertTrue(any(n.x <= x <= n.x + n.w and n.y <= y <= n.y + n.h
                                    for x, y in g.points), f"{t.id} misses {hop}")

    def test_shared_corridor_lanes_are_parallel(self):
        # tunnels to branch1 and branch2 share core1-core2
        mids = []
        for t, g in self.plan:
            if {t.a, t.b} & {"branch1", "branch2"}:
                c1, c2 = self.topo.center("core1"), self.topo.center("core2")
                # the hop's own lane points (25% / 75% along), not the
                # transit points over the routers at either end
                span = c2[0] - c1[0]
                on = [p for p in g.points
                      if c1[0] + 0.1 * span < p[0] < c2[0] - 0.1 * span and abs(p[1] - c1[1]) < 30]
                self.assertEqual(len(on), 2)
                self.assertAlmostEqual(on[0][1], on[1][1])  # horizontal run
                mids.append(on[0][1])
        self.assertEqual(len(mids), 2)
        self.assertGreater(abs(mids[0] - mids[1]), 1)  # in separate lanes

    def test_render_style(self):
        layer = Layer(IPSEC)
        IPSEC.render(self.plan, layer)
        self.assertEqual(len(layer.cells), 3)
        for wrapper in layer.cells:
            style = wrapper.find("mxCell").get("style")
            self.assertIn("curved=1", style)
            self.assertIn("dashed=1", style)
            self.assertIn("exitX=", style)
            self.assertIn("IPsec", wrapper.get("tooltip"))


if __name__ == "__main__":
    unittest.main()
