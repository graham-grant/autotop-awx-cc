# Design archive

Every design document for this project is kept here, so the reasoning behind
each change can still be read after the code has moved on.

## Convention

- Each folder is named for the date its documents were created, as
  `YYYY-MM-DD`.
- A document is never rewritten after its date. When a design changes, the
  new version goes in a new dated folder, under the same file name, with a
  "What changed" section at the top and a link back to the version it
  revises. The older version gets a one-line pointer to the newer one in its
  status line, and nothing else changes.
- Images a document uses sit next to it in the same folder, so each folder
  stands on its own.
- The README at the repository root always describes the current code. This
  archive describes how it got there.

## Index

| Date | Document | Status |
| --- | --- | --- |
| 2026-10-25 | [Multi-vendor device data design](2026-10-25/multi-vendor-device-data-design.md) | Proposed. Superseded by the 2026-10-26 revision. |
| 2026-10-25 | [Copilot rebuild instructions](2026-10-25/copilot-rebuild-instructions.docx) | Snapshot of the code at `main` 27d1889, written as prompts for Microsoft Copilot to regenerate it. |
| 2026-10-26 | [Multi-vendor device data design](2026-10-26/multi-vendor-device-data-design.md) | Accepted. Records the decisions on Windows, Panorama, credentials, the lab and sites. |
| 2026-10-26 | [Read-only collection safeguards](2026-10-26/read-only-collection-safeguards.md) | Implemented for Cisco IOS / IOS-XE. Applies to every later platform. |
| 2026-10-26 | [Phase 1: common device format and Cisco driver](2026-10-26/phase-1-common-format.md) | Implemented. Schema 2 facts, `scripts/drivers/`, IPsec as a facet, segment nodes, VRF reuse rule. |
| 2026-10-26 | [Phase 2: Cisco layer 2](2026-10-26/phase-2-cisco-layer-2.md) | Implemented. LLDP/CDP, ARP and MAC tables, links from evidence, port-channel collapse, VLANs facet and layer. |
| 2026-10-26 | [Phase 3: Palo Alto firewalls](2026-10-26/phase-3-palo-alto.md) | Implemented and tested against a fake XML API; lab firewall check pending. PAN-OS policy, XML API session, direct and Panorama modes, zones layer. |
| 2026-10-26 | [Phase 4: Windows servers](2026-10-26/phase-4-windows.md) | Implemented and tested against a fake OpenSSH server; lab server check pending. PowerShell script policy, PowerShell-over-OpenSSH session, NIC teams, placement by MAC table, server roles layer. |
| 2026-10-31 | [Visio output](2026-10-31/visio-output.md) | Implemented; opens in Visio and draw.io (checked by Jon). Node positions in a Visio template (`template.vsdx`). A format-neutral diagram model written by independent Visio and draw.io writers: pages, layers, icons, tooltips, page links, Shape Data, Visio's schema order. |
