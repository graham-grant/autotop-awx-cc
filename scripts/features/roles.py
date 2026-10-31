"""
Server roles: a small badge on each Windows server's node listing the roles
it runs ("AD DS · DNS · DHCP"), on its own layer. Hovering the badge shows
every installed role by its full name, and the server's domain.

Facts: the "server" facet, which every driver that can supply it fills in
the same shape (the Windows driver from Get-WindowsFeature; see
scripts/drivers/):

    "facets": {"server": {"domain": "lab.local",
                          "roles": [{"name": "DNS", "display_name": "DNS Server"}]}}

  roles[].name          the role's short (feature) name
  roles[].display_name  its full name, for the tooltip

Only roles in SETTINGS["badges"] appear on the badge, under that short
label; a server with none of them gets a tooltip-only badge reading
"roles". The base role every file server has (File and Storage Services)
isn't a badge by default.
"""
from __future__ import annotations

from features import Feature


class RolesFeature(Feature):
    name = "roles"
    label = "Server roles"
    facet = "server"
    SETTINGS = {
        "badges": {
            "AD-Domain-Services": "AD DS", "DNS": "DNS", "DHCP": "DHCP",
            "Web-Server": "IIS", "Hyper-V": "Hyper-V", "ADCS-Cert-Authority": "AD CS",
            "Print-Services": "Print", "RemoteAccess": "RAS", "NPAS": "NPS",
            "WDS": "WDS", "UpdateServices": "WSUS", "ADFS-Federation": "AD FS",
            "Remote-Desktop-Services": "RDS", "FS-FileServer": "File",
        },
        "separator": " · ",
        "style": "text;html=1;align=center;verticalAlign=middle;fontSize=8;rounded=1;"
                 "fillColor=#E1D5E7;strokeColor=#9673A6;fontColor=#000000;spacing=1;",
        "gap": 4.0,             # below the node
    }

    def collect(self, topo):
        found = []  # (host, badge text, tooltip)
        for host in sorted(topo.hosts):
            facet = topo.facet(host, self.facet)
            if not facet or not facet.get("roles") or host not in topo.nodes:
                continue
            badges = self.settings["badges"]
            short = [badges[r["name"]] for r in facet["roles"] if r.get("name") in badges]
            text = self.settings["separator"].join(dict.fromkeys(short)) or "roles"
            lines = [r.get("display_name") or r["name"] for r in facet["roles"]]
            if facet.get("domain"):
                lines.append(f"domain {facet['domain']}")
            found.append((host, text, "\n".join(lines)))
        return found

    def plan(self, found, topo):
        placed = []
        for host, text, tip in found:
            node = topo.nodes[host]
            w = max(40, 6 * len(text))
            placed.append((host, node.x + node.w / 2, node.y + node.h + self.settings["gap"] + 7,
                           text, tip, w))
        return placed

    def render(self, placed, layer):
        for host, x, y, text, tip, w in placed:
            layer.text(host, x, y, text, self.settings["style"], w=w, h=14, tooltip=tip)


FEATURE = RolesFeature()
