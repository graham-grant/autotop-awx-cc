"""
Protocol feature plugins.

The physical diagram (nodes + directly-connected links, routed by
pathfinder.py) is the fixed base. Everything drawn on top of it - IPsec
tunnels today, GRE/MPLS/BGP/etc. later - is a *feature*: one module in
this package exposing a module-level `FEATURE = SomeFeature()`.

Each feature goes through three steps, each with a narrow contract so
features can be added one at a time without touching each other:

  collect(topo)        -> data   read this feature's facet (topo.facet(host,
                                 self.facet)) and the core (topo.hosts)
  plan(data, topo)     -> plan   geometry: where things go, relative to the
                                 already-routed physical diagram
  render(plan, layer)            add lines and labels to this feature's
                                 own layer (toggled in draw.io's Layers
                                 panel, and in Visio's Layer Properties)

Isolation guarantees, enforced by run_features():
- every feature gets its own deep copy of the topology, so it can't
  mutate state another feature (or the base diagram) relies on;
- a feature's items only reach the diagram if all three steps succeed -
  an exception is printed and that feature's layer is skipped, nothing
  else is affected;
- item ids are namespaced by feature name, so two features can't collide.

Features draw into the format-independent diagram (diagram.py), with
style dicts rather than any file format's styles, so the same layer appears
in both the draw.io and the Visio file.

A feature never reads vendor output. It declares the facet it draws
(`facet = "ipsec"`), each platform driver (scripts/drivers/) says which
commands supply that facet and parses them into one vendor-neutral shape,
and collection gathers those commands for every enabled feature - see
scripts/commands.py. Enable/disable features with config.ENABLED_FEATURES,
and override a feature's SETTINGS via config.FEATURE_SETTINGS[name].
"""
from __future__ import annotations

import copy
import importlib
import pkgutil
import traceback

import config
import diagram as dg


class Feature:
    name: str = ""              # unique id: facts key, cell-id prefix, settings key
    label: str = ""             # layer name
    facet: str | None = None    # the facets[...] key this feature draws
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
    """Collects one feature's items; only added to the diagram once the
    feature has finished without errors."""

    def __init__(self, feature: Feature):
        self.prefix = feature.name
        self.id = f"layer-{feature.name}"
        self.label = feature.label or feature.name
        self.items: list = []
        self._ids: set = set()

    def _item_id(self, local_id: str) -> str:
        iid = f"{self.prefix}-{local_id}"
        if iid in self._ids:
            raise ValueError(f"duplicate item id {iid}")
        self._ids.add(iid)
        return iid

    def edge(self, local_id, source, target, style: dict, points=(), exit=None, entry=None,
             value="", tooltip=None):
        """A line between two existing node ids. `points` are (x, y)
        waypoints; exit/entry are optional (fx, fy) perimeter fractions
        pinning where it leaves/meets each node. `style` is a diagram.py
        style dict."""
        line = dg.Line(self._item_id(local_id), style=dict(style), text=value, layer=self.id,
                       tooltip=tooltip, source=source, target=target,
                       waypoints=[(round(x, 2), round(y, 2)) for x, y in points],
                       exit=tuple(exit) if exit else None, entry=tuple(entry) if entry else None)
        self.items.append(line)
        return line.id

    def text(self, local_id, x, y, value, style: dict, w=60, h=16, tooltip=None):
        """A free-standing label centred on (x, y)."""
        shape = dg.Shape(self._item_id(local_id), style={"kind": "text", **style}, text=value,
                         layer=self.id, tooltip=tooltip,
                         x=round(x - w / 2, 2), y=round(y - h / 2, 2), w=w, h=h)
        self.items.append(shape)
        return shape.id

    def attach(self, page: dg.Page):
        clash = [i.id for i in self.items if page.has(i.id)]
        if clash or page.has(self.id):
            raise ValueError(f"id already on the page: {(clash or [self.id])[0]}")
        page.add_layer(dg.Layer(self.id, self.label))
        for item in self.items:
            page.add(item)


def discover() -> dict[str, Feature]:
    """{name: Feature instance} for every module in this package that
    defines FEATURE. A module that fails to import is reported and
    skipped, not fatal."""
    found = {}
    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("_") or mod.name in ("context", "bundling", "porttags"):
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


def facets_enabled() -> list[str]:
    """The facets drawn by the enabled features, in feature order."""
    facets = []
    for feature in enabled():
        if feature.facet and feature.facet not in facets:
            facets.append(feature.facet)
    return facets


def run_features(page: dg.Page, topo, features=None) -> dict[str, str]:
    """Run each feature in isolation and add its layer to the page.
    Returns {feature name: "ok" | "failed: ..."}."""
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
        try:
            layer.attach(page)
        except ValueError as e:   # an id clash with the base diagram
            print(f"features: '{feature.name}' failed and was skipped: {e}")
            results[feature.name] = f"failed: {e}"
            continue
        results[feature.name] = "ok"
        print(f"features: '{feature.name}' added {len(layer.items)} item(s) on layer '{layer.label}'")
    return results
