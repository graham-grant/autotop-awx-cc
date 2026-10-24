#!/usr/bin/env python3
"""
Prints, as a JSON list, every show command the playbook should run on a
host of the given platform: the core commands netparse needs, followed by
whatever each enabled feature plugin declares. Keeps the playbook free of
per-feature edits - adding a feature adds its commands here automatically.

Usage: python3 commands.py <ansible_network_os>
"""
import json
import sys

import features
import netparse


def commands(network_os: str) -> list[str]:
    cmds = netparse.core_commands(network_os)
    for cmd in features.commands_for(netparse.platform_key(network_os)):
        if cmd not in cmds:
            cmds.append(cmd)
    return cmds


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: commands.py <ansible_network_os>")
    print(json.dumps(commands(sys.argv[1])))
