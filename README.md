# Basic Ansible -> drawio topology

Generates a `.drawio` diagram showing how the devices in your Ansible
inventory connect to each other. Links are derived from directly-connected
subnets: any two inventory hosts with an interface IP in the same network
are linked, the same as a directly-connected route would show. Protocol
overlays (IPsec tunnels so far) are drawn on top, each on its own draw.io
layer, by self-contained feature plugins.

**No third-party Python packages.** Everything runs on a base Python 3
install (3.10+): no NAPALM, no PyYAML, nothing to `pip install`. The only
extra requirement is a C compiler and the Python headers, for the A*
router's C extension (see [C extension](#c-extension)).

The diagram-building side of the project lives in `scripts/`:

- `main.py` — the entry point; run this one (`python3 scripts/main.py`)
- `config.py` — paths, excluded networks, styling, router tuning, enabled features
- `netparse/` — parses raw show-command output into the NAPALM-style JSON schema (replaces NAPALM)
- `parser.py` — reads the JSON facts, derives links from subnets
- `pathfinder.py` — the Hanan-grid A* router that finds each link's actual path
- `csrc/_hanan.c` + `build_ext.py` — the A* search in C, and its stdlib-only build script
- `renderer.py` — turns routed nodes/links into draw.io XML, with IP/interface labels and status dots
- `features/` — protocol feature plugins (`ipsec.py`), plus shared overlay geometry (`bundling.py`)
- `commands.py` — tells the playbook which show commands to run per platform
- `miniyaml.py` / `inventory.py` — a small YAML reader (replaces PyYAML) and the shared inventory loader

## Layout

`group_vars/` lives inside `playbooks/`, not at the project root — Ansible
only auto-loads `group_vars/`/`host_vars/` from the directory containing
the playbook file itself (or the inventory file's directory), so it has to
sit next to `gather_topology.yml` for `net_username`/`net_password` and the
`network_devices` SSH settings to resolve.

## How it works

1. `playbooks/gather_topology.yml` connects to every host in the
   `network_devices` group over plain SSH and runs a list of show commands
   with `ansible.builtin.raw` — no NAPALM, no network collections. The
   list comes from `scripts/commands.py`: the core commands for the host's
   platform (`show version`, `show interfaces`, `show ip interface` on
   IOS/IOS-XE) plus whatever each enabled feature needs. The raw output is
   written, one JSON file per host, to `output/facts/`:
   `{"inventory_name", "platform", "cli": {command: output}}`.
2. `scripts/netparse/` parses that output into the same schema NAPALM used
   to produce (`hostname`, `interfaces`, `interfaces_ip`), so the rest of
   the pipeline is unchanged. JSON from the older NAPALM-based playbook
   still loads as-is.
3. `scripts/parser.py` groups every interface's
   IPv4 address by network (address + prefix). Any network shared by
   exactly two inventory hosts becomes a link between them — this is
   exactly what a directly-connected/local route represents, so no LLDP
   or CDP needs to be enabled on the devices at all. A network with only
   one inventory member (e.g. an uplink to a device you don't manage) is
   skipped, since there's no second endpoint to draw. A network shared by
   three or more inventory hosts is drawn as a full mesh between them, with
   a note printed, since that's a shared segment rather than the usual
   point-to-point /30 or /31 design.
   Each link shows the IP address of each end's interface next to that end
   and a small green/red dot indicating whether both ends report the
   interface as up — a side with no status data is treated as up, since
   this is a display simplification, not a real health check.

## Link routing

Links are routed by `pathfinder.py`, a true pathfinding engine rather than
a heuristic: every node edge and port position forms a grid (a Hanan
grid), and each link is routed across it with A* search, one link at a
time, "stamping" its path into a shared congestion map so every
later link can see and avoid the ones already placed. This is what
actually guarantees two links never run on top of each other, rather than
just making it unlikely — links only get bent around each other where the
search genuinely needs to, and stay as straight as the turn penalty allows
otherwise.

A node whose side doesn't have room for all its ports at a safe minimum
spacing is grown automatically — by the exact amount needed, not a rough
estimate — before routing starts. `pathfinder.py` doesn't decide *which*
side of a node a link uses in the first place, though: that choice is
still made the way it always was, by a per-node majority vote over that
node's own links (so a hub with links at slightly different angles ends up
with all of them on one consistent side), with `scripts/layout.yml` able
to override it per node:

```yaml
9500-1: {x: 400, y: 100, side: bottom}
```

Each link's interface ID (abbreviated, e.g. "GigabitEthernet0/1" ->
"Gi0/1") is shown rotated next to each end, close to the parent node. The
IP address label sits a bit further in along the link.

The constants `HANAN_BUFFER` (minimum spacing between lanes),
`HANAN_TURN_PENALTY` (how strongly the router prefers straight runs over
extra bends), and `HANAN_MARGIN` (inset from a node's corners when placing
ports) in `config.py` control routing if the defaults look too tight, too
bendy, or too loose for a particular topology.

## Keeping node positions stable

For a production network with a fixed set of devices, the automatic
force-directed layout can place the same nodes differently between runs —
fine for discovering an unfamiliar topology, but not what you want for a
diagram people reference repeatedly. `scripts/layout.yml` lets you pin
specific nodes to fixed coordinates:

```yaml
core-sw1: {x: 200, y: 150}
core-sw2: {x: 500, y: 150}
```

Nodes listed there are placed at those exact coordinates every run; nodes
not listed fall back to automatic placement (and the script prints a note
naming them, so newly-added devices are easy to spot and pin once you're
happy with where they've landed). The diagram itself is still regenerated
fresh from live data every time — only positions are fixed, not status,
IPs, or link state.

## Protocol features

Everything drawn on top of the physical diagram is a *feature*: one module
in `scripts/features/` with three steps:

| step | input | does |
| --- | --- | --- |
| `collect(topo)` | host JSON (`topo.cli(host, cmd)`) | parse this feature's facts |
| `plan(data, topo)` | parsed data + routed physical diagram | work out geometry |
| `render(plan, layer)` | the plan | add cells to the feature's own draw.io layer |

Features are isolated from each other and from the base diagram, so new
ones can be added one at a time without disturbing finished work:

- each runs on its own copy of the topology and can't change what others see;
- its layer is only added if all three steps succeed — a failure is printed
  and that one feature is left out of the diagram, nothing else changes;
- cell ids are prefixed with the feature name;
- each has its own layer (draw.io **Edit > Layers**), so it can be hidden;
- each declares its own show commands, so the playbook never needs editing;
- each has its own settings dict, overridable in `config.FEATURE_SETTINGS`.

Turn features on and off (and set layer order) with
`config.ENABLED_FEATURES`.

### IPsec tunnels (`features/ipsec.py`)

Reads `show crypto session detail` (IOS/IOS-XE), matches each session's
peer address to the inventory host that owns it, and merges both ends'
reports into one tunnel (drawn red if either end isn't `UP`). The underlay
path is the fewest-hops path over the physical links, and the tunnel is
drawn as a curved, dashed line passing over every router on that path.
Tunnels terminating on the same router converge onto one side of it and run
in evenly spaced parallel lanes before reaching it; tunnels sharing a
physical hop run side by side along it and fan out where their paths split.
Hovering a tunnel shows its endpoints, addresses, status and path.

A host whose data comes from elsewhere can provide tunnels pre-parsed,
optionally with an explicit path, under `"ipsec": {"tunnels": [...]}` in its
JSON (see the module docstring).

### Adding a feature

1. Create `scripts/features/<name>.py` with a `Feature` subclass: set `name`,
   `label`, `commands = {"ios": ["show ..."]}` and `SETTINGS`, implement
   `collect` / `plan` / `render`, and end with `FEATURE = MyFeature()`.
   Overlays that cross routers can reuse `features.bundling.plan_overlays`.
2. Add its name to `config.ENABLED_FEATURES`.
3. Add fixture output to `tests/fixtures/facts/` and a `tests/test_<name>.py`.

## C extension

The A* search at the heart of `pathfinder.py` runs in a CPython C extension
(`scripts/csrc/_hanan.c`), about 80x faster than the equivalent Python on a
mid-sized topology. It's required: `build_ext.py` compiles it using only the
standard library (it asks `sysconfig` for the compiler and flags CPython was
built with), the playbook builds it before touching any device, and
`main.py` rebuilds it automatically if it's missing or out of date. The
build needs `gcc` and the Python headers (`python3-devel` / `python3-dev`)
— add both to your AWX execution environment image. The pure-Python
`astar_route_py` is kept as the readable reference, and the tests check the
two produce identical routes.

## Setup

```bash
pip install ansible-core      # or use AWX; no other packages or collections
python3 scripts/build_ext.py  # needs gcc + Python headers
export NET_USERNAME=admin
export NET_PASSWORD=secret
```

Edit `inventory.yml` with your actual devices — each host needs
`ansible_host` and `ansible_network_os` (`ios` / `iosxe` are supported by
`netparse` today). Devices are reached over SSH; password auth uses
`sshpass` on the controller (already in standard AWX execution
environments). Connection settings are in
`playbooks/group_vars/network_devices.yml`.

## Tests

```bash
python3 -m unittest discover -s tests
```

Fixture show-command output for a six-router topology with three IPsec
tunnels is in `tests/fixtures/`.

## Running this in AWX

Since AWX job containers are ephemeral, don't try to manually copy
`output/facts/*.json` off the execution environment between runs — the
playbook now does the whole pipeline (gather → build → publish) in a single
job. After the gather play writes each host's JSON, a third play runs
`scripts/main.py` and publishes the resulting `topology.drawio` file,
base64-encoded, as an AWX job artifact via `set_stats`.

To get the file after a successful run:
1. Open the completed job in AWX and go to its **Details** tab (or fetch
   `/api/v2/jobs/<id>/` from the API) — look for the `artifacts` field,
   which will contain a key `topology_drawio_b64`.
2. Copy that base64 string, then decode it locally:
   ```bash
   echo "<paste the base64 string>" | base64 -d > topology.drawio
   ```
   or in Python:
   ```python
   import base64
   open("topology.drawio", "wb").write(base64.b64decode("<paste>"))
   ```
3. Open `topology.drawio` in draw.io.

## Running this locally (without AWX)

```bash
ansible-playbook -i inventory.yml playbooks/gather_topology.yml
python3 scripts/main.py
```

Open `output/topology.drawio` in draw.io (desktop app, web app, or the VS
Code extension).

## Notes

- Each host needs at least one interface IP for any link to be drawn at
  all — a device with no `interfaces_ip` data (e.g. only unnumbered
  interfaces, or no `netparse` support for its platform) won't connect to anything in the
  diagram even if it's physically cabled to other inventory hosts.
- `EXCLUDED_NETWORKS` in `config.py` lets you ignore specific subnets
  entirely — e.g. end-host/access VLANs that happen to be configured on a
  router/switch interface but shouldn't be drawn as an inter-device link.
  A more specific subnet within an excluded one (e.g. a /30 carved out of
  an excluded /24) is excluded too.
- This assumes a fairly clean point-to-point addressing scheme (a distinct
  /30 or /31 per link, which is what the directly-connected-route approach
  is built around). A shared multi-access subnet with three or more
  inventory hosts on it still gets drawn (as a full mesh, with a note
  printed), but that's a different physical topology shape than a true
  shared-segment diagram would normally use.
- Platforms other than IOS/IOS-XE (NX-OS, EOS, Junos, PAN-OS, ...) need a
  parser module in `scripts/netparse/` (see its docstring) and, per
  feature, an entry in that feature's `commands`/parsers.
