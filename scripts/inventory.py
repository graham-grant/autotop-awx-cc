"""
Reads inventory.yml once, in one place. parser.py, main.py and
drawio_position_loader.py each used to walk the YAML tree themselves; they
now share host_vars() below, which applies Ansible's group-var inheritance
(a `vars:` block on a group is inherited by every host under it, with a
host's own value winning).

Uses miniyaml rather than PyYAML so the project runs on a base Python
install.
"""
from __future__ import annotations

import miniyaml


def load_tree(inventory_path) -> dict:
    with open(inventory_path, encoding="utf-8") as f:
        return miniyaml.safe_load(f) or {}


def host_vars(inventory_path) -> dict[str, dict]:
    """{hostname: merged vars} for every host under any group's `hosts:`."""
    tree = load_tree(inventory_path)
    result: dict[str, dict] = {}

    def walk(group, inherited):
        if not isinstance(group, dict):
            return
        merged = {**inherited, **(group.get("vars") or {})}
        for name, hvars in (group.get("hosts") or {}).items():
            result[name] = {**result.get(name, {}), **merged, **(hvars or {})}
        for child in (group.get("children") or {}).values():
            walk(child, merged)

    root = tree.get("all") if isinstance(tree.get("all"), dict) else tree
    walk(root, {})
    # Groups defined at the top level beside `all` (valid in Ansible)
    for key, group in tree.items():
        if key != "all" and isinstance(group, dict) and ("hosts" in group or "children" in group):
            walk(group, {})
    return result
