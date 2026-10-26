"""
VLANs: labels each switch port on the diagram with what it carries, on its
own layer - "VLAN 20" for an access port, "trunk 10,20,30" for a trunk.

Facts: the "vlans" facet, which every driver that can supply it fills in
the same shape (the Cisco driver from `show vlan brief` and `show
interfaces trunk`; see scripts/drivers/):

    "facets": {"vlans": {
        "vlans":  [{"id": 20, "name": "USERS", "ports": ["GigabitEthernet1/0/5"]}],
        "trunks": [{"port": "Port-channel1", "native": 1,
                    "allowed": "1-4094", "active": "1,10,20"}]}}

  vlans[].ports     access ports in the VLAN (interface names as in the core)
  trunks[].allowed  VLANs allowed on the trunk, as a range string
  trunks[].active   VLANs allowed and active on it; the label falls back to
                    "allowed" when a platform doesn't report this

A label sits on the link, a short way out from the switch's end of it.
Segment nodes already carry their subnet's VLAN, so links to them aren't
labelled.
"""
from __future__ import annotations

from features.porttags import PortTagFeature


class VlansFeature(PortTagFeature):
    name = "vlans"
    label = "VLANs"
    facet = "vlans"
    SETTINGS = {
        "offset": 34.0,         # how far out along the link from the switch's node
        "max_chars": 24,        # longer trunk lists are cut short with "..."
        "style": "text;html=1;align=center;verticalAlign=middle;fontSize=8;"
                 "labelBackgroundColor=#FFF2CC;fontColor=#7F6000;spacing=1;",
    }

    def port_label(self, facet: dict, port: str) -> tuple[str, str] | None:
        """(label, tooltip) for one port, or None if the facet says nothing
        about it."""
        for t in facet.get("trunks", []):
            if t.get("port") == port:
                text = t.get("active") or t.get("allowed") or ""
                tip = f"{port}: trunk, native VLAN {t.get('native')}, VLANs {text or 'none'}"
                if len(text) > self.settings["max_chars"]:
                    text = text[:self.settings["max_chars"] - 3].rsplit(",", 1)[0] + ",..."
                return f"trunk {text}".strip(), tip
        for v in facet.get("vlans", []):
            if port in v.get("ports", []):
                return f"VLAN {v['id']}", f"{port}: access, VLAN {v['id']} ({v.get('name', '')})"
        return None


FEATURE = VlansFeature()
