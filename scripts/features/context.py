"""
Topology: the read-only view of the base diagram every feature works from.
Built once by main.py after the physical links are routed, then deep-copied
per feature by run_features().
"""
from __future__ import annotations

import heapq
import ipaddress

import drivers

# Down links still count for path-finding (a tunnel's real path is unknown
# anyway) but only as a last resort.
_DOWN_LINK_COST = 1000


class Topology:
    def __init__(self, hosts: list[dict], edges: list[dict], nodes, specs=(), segments=()):
        self.hosts = {drivers.name(h): h for h in hosts}  # schema 2 records
        self.edges = list(edges)
        self.segments = {s["id"]: s for s in segments}
        self.nodes = {n.id: n for n in nodes}
        self.specs = {s.id: s for s in specs}

        # Every interface address on every host - including excluded
        # networks and loopbacks, since tunnels commonly terminate there.
        self.ip_owner: dict[str, tuple[str, str]] = {}
        for name, host in self.hosts.items():
            for intf in host.get("interfaces", []):
                for addr in (intf.get("ipv4") or []) + (intf.get("ipv6") or []):
                    self.ip_owner.setdefault(_norm_ip(addr), (name, intf["name"]))

        self.adjacency: dict[str, list[tuple[str, dict]]] = {n: [] for n in self.hosts}
        for e in self.edges:
            self.adjacency.setdefault(e["source"], []).append((e["target"], e))
            self.adjacency.setdefault(e["target"], []).append((e["source"], e))

    def platform(self, host: str) -> str:
        return drivers.platform_key(self.hosts[host]["device"].get("platform"))

    def facet(self, host: str, facet: str) -> dict | None:
        """The host's vendor-neutral data for one facet ("ipsec", ...), or
        None if its driver didn't supply it."""
        return (self.hosts[host].get("facets") or {}).get(facet)

    def owner_of_ip(self, addr: str) -> tuple[str, str] | None:
        return self.ip_owner.get(_norm_ip(addr))

    def center(self, name: str) -> tuple[float, float]:
        n = self.nodes[name]
        return n.x + n.w / 2, n.y + n.h / 2

    def shortest_path(self, src: str, dst: str) -> list[str] | None:
        """Fewest-hops path over physical links (up links preferred),
        ties broken by host name so output is stable run to run. A path
        across a shared segment includes the segment node's id."""
        if src == dst:
            return [src]
        best = {src: 0}
        prev: dict[str, str] = {}
        heap = [(0, [src], src)]
        while heap:
            cost, trail, here = heapq.heappop(heap)
            if here == dst:
                return trail
            if cost > best.get(here, float("inf")):
                continue
            for nbr, edge in sorted(self.adjacency.get(here, []), key=lambda t: t[0]):
                step = 1 if edge.get("is_up", True) else _DOWN_LINK_COST
                new = cost + step
                if new < best.get(nbr, float("inf")):
                    best[nbr] = new
                    prev[nbr] = here
                    heapq.heappush(heap, (new, trail + [nbr], nbr))
        return None


def _norm_ip(addr: str) -> str:
    addr = addr.split("/")[0]
    try:
        return str(ipaddress.ip_address(addr))
    except ValueError:
        return addr
