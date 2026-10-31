"""
Firewall zones: labels each firewall end of a link with the security zone
its interface is in ("untrust", "trust"), on its own layer, each zone in
its own colour.

Facts: the "zones" facet, which every driver that can supply it fills in
the same shape (the PAN-OS driver from `show interface all`; see
scripts/drivers/):

    "facets": {"zones": {"zones": [
        {"name": "trust", "vsys": "vsys1", "interfaces": ["ethernet1/2"]}]}}

  zones[].vsys        the virtual system (or context) the zone belongs to
  zones[].interfaces  logical interfaces in the zone

A link usually ends on a physical port, while zones hold its logical
interfaces: a port whose subinterfaces (ethernet1/4.20, ethernet1/4.30)
are in several zones is labelled with all of them, "dmz, guest".

Colours: config.FEATURE_SETTINGS["zones"]["colors"] maps a zone name to a
fill colour; other zones get one from the palette, chosen by name, so a
zone keeps its colour on every firewall and every run.
"""
from __future__ import annotations

import zlib

from features.porttags import PortTagFeature


class ZonesFeature(PortTagFeature):
    name = "zones"
    label = "Firewall zones"
    facet = "zones"
    SETTINGS = {
        "offset": 34.0,          # how far out along the link from the firewall's node
        "style": {"kind": "text", "font_size": 8, "rounded": True, "font_color": "#000000",
                  "stroke": "#666666", "spacing": 1},
        "colors": {"untrust": "#F8CECC", "trust": "#D5E8D4"},
        "palette": ["#DAE8FC", "#FFE6CC", "#E1D5E7", "#FFF2CC", "#F5F5F5", "#D0CEE2",
                    "#B1DDF0", "#FAD9D5"],
    }

    def color(self, zone: str) -> str:
        colors = self.settings["colors"]
        if zone in colors:
            return colors[zone]
        palette = self.settings["palette"]
        return palette[zlib.crc32(zone.encode()) % len(palette)]

    def port_label(self, facet: dict, port: str):
        """(label, tooltip, extra style: the zone's fill) for one port, or
        None if none of its interfaces is in a zone."""
        found = []
        for z in facet.get("zones", []):
            for intf in z.get("interfaces", []):
                if intf == port or intf.startswith(port + "."):
                    found.append((z["name"], z.get("vsys"), intf))
        if not found:
            return None
        names = list(dict.fromkeys(name for name, _, _ in found))
        tip = "; ".join(f"{intf}: zone {name}" + (f" ({vsys})" if vsys else "")
                        for name, vsys, intf in found)
        fill = self.color(names[0]) if len(names) == 1 else "#FFFFFF"
        return ", ".join(names), tip, {"fill": fill}


FEATURE = ZonesFeature()
