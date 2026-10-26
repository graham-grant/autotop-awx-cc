"""
Helpers shared by the platform drivers and by link inference: MAC and
interface-name normalisation. Nothing vendor-specific beyond the common
abbreviation habit ("Gi1/0/1" for "GigabitEthernet1/0/1").
"""
from __future__ import annotations

import re

_HEX = re.compile(r"[0-9a-fA-F]")
_NAME = re.compile(r"^([A-Za-z][A-Za-z-]*?)\s*(\d\S*)$")


def norm_mac(text: str | None) -> str | None:
    """Any common MAC spelling ('5254.0012.3456', '52-54-00-12-34-56',
    '52:54:00:12:34:56') -> '52:54:00:12:34:56', or None if it isn't one."""
    if not text:
        return None
    digits = "".join(_HEX.findall(text))
    if len(digits) != 12 or re.search(r"[^0-9a-fA-F.:\-\s]", text):
        return None
    digits = digits.lower()
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))


def canonical_intf(name: str | None, known) -> str | None:
    """Resolve an abbreviated interface name against a device's own
    interface names: 'Gi1/0/1' -> 'GigabitEthernet1/0/1', 'Po1' ->
    'Port-channel1'. Exact matches win; an abbreviation must match exactly
    one known name. Otherwise the name is returned unchanged."""
    if not name:
        return name
    known = list(known)
    if name in known:
        return name
    lower = {k.lower(): k for k in known}
    if name.lower() in lower:
        return lower[name.lower()]
    m = _NAME.match(name.strip())
    if not m:
        return name
    prefix, rest = m.group(1).lower(), m.group(2)
    hits = []
    for k in known:
        km = _NAME.match(k)
        if km and km.group(2) == rest and km.group(1).lower().startswith(prefix):
            hits.append(k)
    return hits[0] if len(hits) == 1 else name
