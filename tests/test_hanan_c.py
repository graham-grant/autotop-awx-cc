"""The C A* (csrc/_hanan.c) must produce exactly what the pure-Python
reference (pathfinder.astar_route_py) does."""
import random
import unittest
from unittest import mock

import support  # noqa: F401
import pathfinder
from pathfinder import Node


def random_topology(seed, n_nodes=7, n_links=10):
    rng = random.Random(seed)
    centers, names = {}, [f"n{i}" for i in range(n_nodes)]
    for i, name in enumerate(names):
        centers[name] = (200 * (i % 3) + rng.randint(-30, 30), 160 * (i // 3) + rng.randint(-20, 20))
    edges, seen = [], set()
    while len(edges) < n_links:
        a, b = rng.sample(names, 2)
        if (a, b) in seen or (b, a) in seen:
            continue
        seen.add((a, b))
        edges.append({"id": f"link_{len(edges)}", "source": a, "target": b})
    sizes = {n: (120, 40) for n in names}
    return edges, centers, sizes


def route(edges, centers, sizes, search):
    with mock.patch.object(pathfinder, "astar_route", search):
        nodes, specs = pathfinder.build_link_specs(edges, centers, sizes)
    return [(s.id, s.src_frac, s.dst_frac, [(g.p1, g.p2) for g in s.segments]) for s in specs]


class HananCParityTest(unittest.TestCase):
    def test_extension_loaded(self):
        self.assertTrue(pathfinder._hanan.__file__.endswith(
            __import__("sysconfig").get_config_var("EXT_SUFFIX")))

    def test_identical_routes(self):
        for seed in range(12):
            with self.subTest(seed=seed):
                topo = random_topology(seed)
                c = route(*topo, pathfinder.astar_route)
                py = route(*topo, pathfinder.astar_route_py)
                self.assertEqual(c, py)

    def test_soft_mode_and_no_path(self):
        grid = pathfinder.HananGrid([Node("a", 0, 0, 40, 40), Node("b", 200, 0, 40, 40)], [], 15.0, 30.0)
        cong = pathfinder.CongestionMap()
        start = [pathfinder.Point(40.0, 20.0)]
        goal = [pathfinder.Point(200.0, 20.0)]
        # a blocking parallel segment right on the straight line
        cong.add_path("x", "z", [pathfinder.Point(40.0, 20.0), pathfinder.Point(200.0, 20.0)])
        for hard in (True, False):
            args = (grid, cong, start, goal, "b", 15.0, 500.0, 2.5, hard)
            self.assertEqual(pathfinder.astar_route(*args), pathfinder.astar_route_py(*args))
        # goal unreachable: fully enclosed
        boxed = pathfinder.HananGrid([Node("a", 0, 0, 40, 40)], [], 15.0, 0.0)
        self.assertIsNone(pathfinder.astar_route(
            boxed, pathfinder.CongestionMap(), [pathfinder.Point(0.0, 0.0)],
            [pathfinder.Point(20.0, 20.0)], "a", 15.0, 1.0, 1.0, True))


if __name__ == "__main__":
    unittest.main()
