# Basic Ansible -> drawio topology

Generates a `.drawio` diagram showing how the devices in your Ansible
inventory connect to each other. Links are derived from directly-connected
subnets: any two inventory hosts with an interface IP in the same network
are linked, the same as a directly-connected route would show. Protocol
overlays (IPsec tunnels so far) are drawn on top, each on its own draw.io
layer, by self-contained feature plugins.

**No third-party Python packages.** Everything runs on a base Python 3
install (3.9+): no NAPALM, no PyYAML, nothing to `pip install`, and no
compiler. The A* router's C extension ships prebuilt (see
[C extension](#c-extension)).

The diagram-building side of the project lives in `scripts/`:

- `main.py` — the entry point; run this one (`python3 scripts/main.py`)
- `config.py` — paths, excluded networks, styling, router tuning, enabled features
- `netparse/` — parses raw show-command output into the NAPALM-style JSON schema (replaces NAPALM)
- `parser.py` — reads the JSON facts, derives links from subnets
- `pathfinder.py` — the Hanan-grid A* router that finds each link's actual path
- `csrc/_hanan.c` + `_hanan.abi3.so` + `build_ext.py` — the A* search in C, its prebuilt binary, and the stdlib-only build script
- `renderer.py` — turns routed nodes/links into draw.io XML, with IP/interface labels and status dots
- `features/` — protocol feature plugins (`ipsec.py`), plus shared overlay geometry (`bundling.py`)
- `collect.py` / `sshcli.py` — collects a device's show output over one interactive SSH session (system `ssh` + pty)
- `commands.py` — the show commands to run per platform (core + enabled features)
- `miniyaml.py` / `inventory.py` — a small YAML reader (replaces PyYAML) and the shared inventory loader

## Layout

`group_vars/` lives inside `playbooks/`, not at the project root — Ansible
only auto-loads `group_vars/`/`host_vars/` from the directory containing
the playbook file itself (or the inventory file's directory), so it has to
sit next to `gather_topology.yml` for `net_username`/`net_password` and the
`network_devices` SSH settings to resolve.

## How it works

1. `playbooks/gather_topology.yml` runs `scripts/collect.py` on the
   controller once per host in the `network_devices` group. It opens one
   interactive SSH session per device, using the system `ssh` client
   driven through a pseudo-terminal (`scripts/sshcli.py`, stdlib only), and
   runs a list of show commands, the way NAPALM did — no NAPALM, no
   network collections, no `sshpass`. It answers password prompts, in-session
   `User Name:`/`Password:` logins, pagers and (with `net_enable_password`)
   `enable`. On a failed login, it prints what the device actually replied.
   The command list comes from `scripts/commands.py`: the core
   commands for the host's platform (`show version`, `show interfaces`,
   `show ip interface` on IOS/IOS-XE) plus whatever each enabled feature
   needs. The raw output is written, one JSON file per host, to
   `output/facts/`: `{"inventory_name", "platform", "cli": {command: output}}`.
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
mid-sized topology. It's written against CPython's stable ABI, so one
compiled file, `scripts/_hanan.abi3.so`, works on every CPython 3.9+ on
Linux x86_64 (it needs nothing newer than glibc 2.4). That file is
**prebuilt and committed**, so AWX execution environments need no compiler
or Python headers. Its build was tested in Rocky Linux 9 with Python 3.9,
the same base as the stock AWX EE, with no compiler installed.

If you change `csrc/_hanan.c`, rebuild the binary and commit it with the
source. You'll need gcc and the Python headers on the machine you build
on, but not on AWX:

```bash
python3 scripts/build_ext.py           # stdlib-only build
python3 scripts/build_ext.py --check   # is the committed binary current?
```

The build stamps the source's SHA-256 into the binary. The tests fail if
the committed binary doesn't match the source. At runtime, a stale binary is
rebuilt if a compiler is available; otherwise it's used with a warning. On a
platform other than Linux x86_64 (e.g. an ARM EE), run `build_ext.py`
there once. The pure-Python `astar_route_py` is kept as the readable
reference, and the tests check the two produce identical routes.

## Setup

```bash
pip install ansible-core      # or use AWX; no other packages or collections
export NET_USERNAME=admin
export NET_PASSWORD=secret
```

Edit `inventory.yml` with your actual devices — each host needs
`ansible_host` and `ansible_network_os` (`ios` / `iosxe` are supported by
`netparse` today). Credentials are read from `NET_USERNAME` /
`NET_PASSWORD`, or from `NAPALM_USERNAME` / `NAPALM_PASSWORD`, so an existing
AWX custom credential type that injects those keeps working unchanged.
An optional enable secret and extra `ssh` options (e.g. legacy key exchange)
are set in `playbooks/group_vars/network_devices.yml`. The controller needs
the OpenSSH client, which every AWX execution environment includes.

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
