"""
Protocol feature plugins.

The physical diagram (nodes + directly-connected links, routed by
pathfinder.py) is the fixed base. Everything drawn on top of it - IPsec
tunnels today, GRE/MPLS/BGP/etc. later - is a *feature*: one module in
this package exposing a module-level `FEATURE = SomeFeature()`.

Each feature goes through three steps, each with a narrow contract so
features can be added one at a time without touching each other:

  collect(topo)        -> data   parse this feature's facts out of the host
                                 JSON (topo.cli(host, cmd) / topo.hosts)
  plan(data, topo)     -> plan   geometry: where things go, relative to the
                                 already-routed physical diagram
  render(plan, layer)            emit cells into this feature's own
                                 draw.io layer (toggle it in Edit > Layers)

Isolation guarantees, enforced by run_features():
- every feature gets its own deep copy of the topology, so it can't
  mutate state another feature (or the base diagram) relies on;
- a feature's cells only reach the diagram if all three steps succeed -
  an exception is printed and that feature's layer is skipped, nothing
  else is affected;
- cell ids are namespaced by feature name, so two features can't collide.

A feature also declares which CLI commands it needs per platform
(`commands`), which the playbook gathers alongside the core ones - see
scripts/commands.py. Enable/disable features with config.ENABLED_FEATURES,
and override a feature's SETTINGS via config.FEATURE_SETTINGS[name].
"""
from __future__ import annotations

import copy
import importlib
import pkgutil
import traceback
import xml.etree.ElementTree as ET

import config


class Feature:
    name: str = ""              # unique id: facts key, cell-id prefix, settings key
    label: str = ""             # draw.io layer name
    commands: dict[str, list[str]] = {}  # platform -> show commands to gather
    SETTINGS: dict = {}         # defaults, overridable via config.FEATURE_SETTINGS

    def __init__(self):
        overrides = getattr(config, "FEATURE_SETTINGS", {}).get(self.name, {})
        self.settings = {**self.SETTINGS, **overrides}

    def collect(self, topo):
        raise NotImplementedError

    def plan(self, data, topo):
        return data

    def render(self, plan, layer: "Layer"):
        raise NotImplementedError


class Layer:
    """Collects one feature's cells; only attached to the diagram once the
    feature has finished without errors."""

    def __init__(self, feature: Feature):
        self.prefix = feature.name
        self.id = f"layer-{feature.name}"
        self.label = feature.label or feature.name
        self.cells: list[ET.Element] = []
        self._ids: set[str] = set()

    def _cell_id(self, local_id: str) -> str:
        cid = f"{self.prefix}-{local_id}"
        if cid in self._ids:
            raise ValueError(f"duplicate cell id {cid}")
        self._ids.add(cid)
        return cid

    def edge(self, local_id, source, target, style, points=(), exit=None, entry=None,
             value="", tooltip=None):
        """A draw.io edge between two existing node ids. `points` are
        (x, y) waypoints; exit/entry are optional (fx, fy) perimeter
        fractions pinning where the edge leaves/meets each node."""
        cid = self._cell_id(local_id)
        if exit:
            style += f"exitX={exit[0]};exitY={exit[1]};exitDx=0;exitDy=0;exitPerimeter=0;"
        if entry:
            style += f"entryX={entry[0]};entryY={entry[1]};entryDx=0;entryDy=0;entryPerimeter=0;"
        attrs = {"style": style, "edge": "1", "source": source, "target": target,
                 "parent": self.id}
        if tooltip:
            wrapper = ET.Element("object", {"id": cid, "label": value, "tooltip": tooltip})
            cell = ET.SubElement(wrapper, "mxCell", attrs)
        else:
            wrapper = cell = ET.Element("mxCell", {"id": cid, "value": value, **attrs})
        geom = ET.SubElement(cell, "mxGeometry", {"relative": "1", "as": "geometry"})
        if points:
            arr = ET.SubElement(geom, "Array", {"as": "points"})
            for x, y in points:
                ET.SubElement(arr, "mxPoint", {"x": f"{x:.2f}", "y": f"{y:.2f}"})
        self.cells.append(wrapper)
        return cid

    def attach(self, root: ET.Element):
        ET.SubElement(root, "mxCell", {"id": self.id, "value": self.label, "parent": "0"})
        root.extend(self.cells)


def discover() -> dict[str, Feature]:
    """{name: Feature instance} for every module in this package that
    defines FEATURE. A module that fails to import is reported and
    skipped, not fatal."""
    found = {}
    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("_") or mod.name in ("context", "bundling"):
            continue
        try:
            module = importlib.import_module(f"{__name__}.{mod.name}")
        except Exception:
            print(f"features: failed to import '{mod.name}', skipping:\n{traceback.format_exc()}")
            continue
        feature = getattr(module, "FEATURE", None)
        if isinstance(feature, Feature):
            found[feature.name] = feature
    return found


def enabled() -> list[Feature]:
    """Features listed in config.ENABLED_FEATURES, in that order (None =
    every discovered feature, alphabetically)."""
    available = discover()
    wanted = getattr(config, "ENABLED_FEATURES", None)
    if wanted is None:
        return [available[n] for n in sorted(available)]
    missing = [n for n in wanted if n not in available]
    if missing:
        print(f"features: enabled but not found: {missing}")
    return [available[n] for n in wanted if n in available]


def commands_for(platform: str) -> list[str]:
    cmds = []
    for feature in enabled():
        for cmd in feature.commands.get(platform, []):
            if cmd not in cmds:
                cmds.append(cmd)
    return cmds


def run_features(root: ET.Element, topo, features=None) -> dict[str, str]:
    """Run each feature in isolation and attach its layer to the diagram's
    <root>. Returns {feature name: "ok" | "failed: ..."}."""
    results = {}
    for feature in (features if features is not None else enabled()):
        layer = Layer(feature)
        try:
            own = copy.deepcopy(topo)
            data = feature.collect(own)
            plan = feature.plan(data, own)
            feature.render(plan, layer)
        except Exception as e:
            print(f"features: '{feature.name}' failed and was skipped:\n{traceback.format_exc()}")
            results[feature.name] = f"failed: {e}"
            continue
        layer.attach(root)
        results[feature.name] = "ok"
        print(f"features: '{feature.name}' added {len(layer.cells)} cell(s) on layer '{layer.label}'")
    return results
