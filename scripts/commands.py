#!/usr/bin/env python3
"""
Prints, as a JSON list, every show command the playbook should run on a
host of the given platform: the core commands its driver needs, followed by
the commands behind each facet an enabled feature draws. Keeps the playbook
free of per-feature edits - enabling a feature adds its commands here
automatically.

Usage: python3 commands.py <ansible_network_os>
"""
import json
import sys

import drivers
import features


def commands(network_os: str) -> list[str]:
    cmds = drivers.core_commands(network_os)
    for cmd in drivers.facet_commands(network_os, features.facets_enabled()):
        if cmd not in cmds:
            cmds.append(cmd)
    return cmds


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: commands.py <ansible_network_os>")
    print(json.dumps(commands(sys.argv[1])))
