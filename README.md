# autotop-awx: network topology diagrams from AWX

This project logs in to the network devices in an Ansible inventory, collects
their show-command output, and draws a draw.io diagram of how they connect.
It runs as a single AWX job (or `ansible-playbook` by hand) and needs nothing
beyond Ansible and a base Python 3.9+ install: no NAPALM, no Ansible
collections, no PyYAML, nothing to `pip install`, and no compiler.

Collection is strictly read-only. Every command sent to a device is checked
against a per-platform allowlist before it is sent, recorded, and verified
again after the run, and a configuration change during collection fails the
job. See [Read-only collection](#read-only-collection).

The diagram is built in two parts:

- **The physical diagram.** One node per inventory device, and one link for
  every subnet two devices share. Links are routed around the nodes so they
  never overlap, and each end is labelled with its interface, coloured green
  or red by link status.
- **Feature layers.** Everything else (IPsec tunnels today; VRFs, VLANs,
  firewall zones and so on later) is drawn by an independent feature plugin
  onto its own draw.io layer. Each layer can be shown or hidden in draw.io
  (**View > Layers**, or Ctrl+Shift+L), so a viewer turns on only what's
  relevant to them.

Contents:

1. [Repository layout](#repository-layout)
2. [Setup](#setup)
3. [Data collection, step by step](#data-collection-step-by-step)
4. [Read-only collection](#read-only-collection)
5. [Building the diagram, step by step](#building-the-diagram-step-by-step)
6. [Rendering features](#rendering-features)
7. [Configuration reference](#configuration-reference)
8. [The C extension](#the-c-extension)
9. [Tests](#tests)
10. [Limitations and scaling](#limitations-and-scaling)
11. [Design history](#design-history)

## Repository layout

| Path | What it is |
| --- | --- |
| `inventory.yml` | The Ansible inventory: devices in `network_devices`, the file server in `artifact_server` |
| `playbooks/gather_topology.yml` | The whole pipeline: collect, build, publish, audit |
| `playbooks/group_vars/` | Credential lookups (`all.yml`) and SSH options (`network_devices.yml`) |
| `docs/Prod_Library.xml` | draw.io shape library holding the device icons |
| `docs/design-archive/` | Design documents, one dated folder per revision |
| `CLAUDE.md` | Standing rules for anyone (or any assistant) changing the code |
| `scripts/collect.py` | Logs in to one device and saves its show-command output as JSON |
| `scripts/sshcli.py` | Interactive SSH session driver (system `ssh` client + a pseudo-terminal) |
| `scripts/commands.py` | Works out which show commands to run on a device |
| `scripts/readonly.py` | The read-only command policy every command must pass before it is sent |
| `scripts/audit.py` | Re-checks the commands recorded for every device after collection |
| `scripts/netparse/` | Parses raw show output into structured interface data, one module per platform |
| `scripts/main.py` | Entry point for building the diagram |
| `scripts/parser.py` | Loads the JSON files and derives links from shared subnets |
| `scripts/drawio_position_loader.py` | Reads and writes node positions in `template.drawio` |
| `scripts/pathfinder.py` | The link router (Hanan grid + A* search) |
| `scripts/csrc/_hanan.c`, `scripts/_hanan.abi3.so`, `scripts/build_ext.py` | The A* search in C, its prebuilt binary, and its build script |
| `scripts/renderer.py` | Writes nodes, links and interface labels as draw.io XML |
| `scripts/icon_library.py` | Reads the icon library so nodes can show device pictures |
| `scripts/features/` | Feature plugins (`ipsec.py`) and their shared helpers |
| `scripts/inventory.py`, `scripts/miniyaml.py` | Inventory reader and the small YAML parser it uses |
| `scripts/config.py` | All tunable settings |
| `tests/` | Unit and end-to-end tests with fixture device output |

## Setup

### Inventory

Each device goes under `network_devices` with its address and platform:

```yaml
all:
  children:
    network_devices:
      hosts:
        core-rtr1:
          ansible_host: 10.0.0.1
          ansible_network_os: ios     # ios / iosxe today
          icon: "ASR1004 Front"       # optional: a title from docs/Prod_Library.xml
          w: 230                      # optional node size
          h: 42
    artifact_server:
      hosts:
        diagram-host:
          ansible_host: 192.168.10.175
```

`icon`, `w` and `h` can also be set once for a whole group under `vars:`.
A host-level value wins over a group value.

### Credentials

Credentials are read from the job's environment and are never stored in the
repository or passed on a command line.

- **In AWX**, attach a credential of the built-in **Network** type to the job
  template. It sets `ANSIBLE_NET_USERNAME` and `ANSIBLE_NET_PASSWORD`. If the
  devices need an enable secret, fill in "Authorize password", which sets
  `ANSIBLE_NET_AUTH_PASS`. No custom credential type is needed.
- **By hand**, export `NET_USERNAME` and `NET_PASSWORD` (and optionally
  `NET_ENABLE_PASSWORD`).

The older `NAPALM_USERNAME` / `NAPALM_PASSWORD` names are still accepted.

### SSH options

Devices that only offer old key-exchange or host-key algorithms need extra
`ssh` options. Set them in `playbooks/group_vars/network_devices.yml`:

```yaml
net_ssh_args: "-o KexAlgorithms=+diffie-hellman-group14-sha1 -o HostKeyAlgorithms=+ssh-rsa"
```

`group_vars/` has to sit next to the playbook, because that's one of the
places Ansible looks for it.

## Data collection, step by step

`playbooks/gather_topology.yml` runs six plays in order.

**Play 1: prepare (localhost).**
It creates `output/facts/`, then imports `pathfinder.py` to confirm the
prebuilt C extension loads. It then prints the read-only command plan for
each platform in the inventory (`collect.py --plan`): every command that
will be sent, each checked against the read-only policy. If the C extension
can't load, a platform has no read-only policy, or a planned command would
be refused, the job stops here, before any device is contacted.

**Play 2: collect (every host in `network_devices`).**
Ansible doesn't connect to the devices itself (`connection: local`). For each
device, Ansible's normal forks run `scripts/collect.py` on the controller,
which does the following:

1. It checks that a username and password were found. If not, the play fails
   with a message telling you to attach the Network credential.
2. It asks `commands.py` which commands to run. That list is the core
   commands for the device's platform plus every command an enabled feature
   declares. On IOS / IOS-XE that's currently `show version`,
   `show interfaces`, `show ip interface` and `show crypto session detail`.
3. It opens one interactive SSH session to the device with `sshcli.py`. This
   runs the system `ssh` client through a pseudo-terminal and replies to
   prompts the way a person would: the SSH password prompt, a second
   `User Name:` / `Password:` login inside the session, `--More--` pagers,
   and `enable` if the device lands at a `>` prompt and an enable secret was
   given. If the login is refused, the error shows what the device actually
   replied, with passwords masked.
4. It turns off paging (`terminal length 0`) and reads the configuration
   change marker (`show running-config | include configuration change`).
   Then it runs each command. A command the device rejects is skipped and
   the rest still run. Finally it reads the marker again.
5. Every line it sends is checked against the read-only policy first and
   printed in the job log as it is sent.
6. It writes the raw output to `output/facts/<inventory name>.json`, with a
   record of every line sent and the result of the configuration check:

   ```json
   {"inventory_name": "core-rtr1", "platform": "ios",
    "cli": {"show version": "...", "show interfaces": "...", "...": "..."},
    "audit": {"policy": "ios",
              "commands": [{"time": "2026-10-26T01:00:03+00:00", "command": "show version",
                            "kind": "collect", "status": "ok"}, "..."],
              "config_check": {"status": "unchanged", "command": "...",
                               "before": "! Last configuration change at ...",
                               "after": "! Last configuration change at ..."}}}
   ```

   If the marker changed, the file is still written, and the host fails with
   exit code 3 and a `CONFIGURATION CHANGED DURING COLLECTION` message.

Collection is the only step that talks to the devices. Everything after it,
features included, works from these JSON files.

**Play 3: copy the facts (`artifact_server`).**
It copies `output/facts/` to the artifact server, so the raw data outlives
the AWX job container.

**Play 4: build (localhost).**
It runs `scripts/main.py output/facts output` and prints its output in the
job log. The next section covers what it does.

**Play 5: publish (`artifact_server`).**
It copies `output/topology.drawio` to the artifact server.

**Play 6: audit (localhost).**
It runs `scripts/audit.py output/facts`, which re-checks every recorded
command in every facts file against the read-only policy, independently of
the check made when it was sent, and prints one line per device. The job
fails if any command was out of policy, refused or cancelled, if a
configuration changed, or if a facts file has no audit record. It runs last,
so the diagram is still published.

## Read-only collection

Collection must never change a device's configuration. Every command it
sends is only for gathering information, and that is enforced in code, not
left to the command lists. The full design is in
[docs/design-archive/2026-10-26/read-only-collection-safeguards.md](docs/design-archive/2026-10-26/read-only-collection-safeguards.md).

| Safeguard | Where | What it does |
| --- | --- | --- |
| Allowlist per platform | `scripts/readonly.py` | On IOS, only `show ...` with display filters after a pipe (`include`, `exclude`, `begin`, `section`, `count`), plus `terminal length 0`, `terminal width N` and `terminal datadump`, which affect only the current session. `show ... \| redirect`, `tee` and `append` are refused, because they write files. |
| Denylist of state-changing verbs | `scripts/readonly.py` | A command starting with, or abbreviating, a verb such as `configure`, `write`, `copy`, `reload`, `delete`, `erase`, `clear`, `debug`, `set`, `commit`, `request` or a PowerShell `Set-`/`New-`/`Remove-` verb is refused, even if an allowlist entry were wrong. |
| Character check | `scripts/readonly.py` | Newlines, control characters, tabs and `?` are refused, so nothing can smuggle a second command onto the line. |
| Fail closed | `scripts/readonly.py`, `collect.py` | A platform without a policy is never contacted. |
| Enforced at the lowest layer | `scripts/sshcli.py` | `CliSession.run()` refuses to send anything the policy doesn't allow, and a session with no policy sends nothing. The only other input ever typed is the login answers, `enable` and `exit`. |
| Confirmations are never answered | `scripts/sshcli.py` | If a device asks `[confirm]`, `[yes/no]`, `(y/n)` or `...]?`, the session sends Ctrl-C, stops using the session and fails the host. A command that times out is also cancelled with Ctrl-C before anything else is sent. |
| Plan before connecting | `collect.py --plan`, play 1 | Prints every command per platform and stops the job if any would be refused. |
| Audit trail | `sshcli.py`, `collect.py` | Every line sent is printed in the job log and stored with a timestamp and outcome in the facts file. |
| Configuration-change check | `collect.py`, `netparse/ios.py` | The IOS `Last configuration change` marker is read before and after collection. If it moved, the host fails loudly. If it can't be read (for example, an account below privilege 15), a warning says so. |
| Verification after the run | `scripts/audit.py`, play 6 | Re-checks every recorded command and the configuration check, and fails the job on any problem. |
| Command inventory test | `tests/test_readonly.py` | Checks every command any driver or feature can emit, enabled or not, against its platform's policy, and checks a list of dangerous commands is refused. A new command that isn't read-only fails the tests. |

To see what would be sent without contacting anything:

```bash
python3 scripts/collect.py --plan --platform ios
```

The strongest guarantee is on the device side, and is recommended in
production: give the collection account read-only rights, such as an IOS
privilege level or TACACS+ command authorisation that permits only `show`
and `terminal`, a PAN-OS read-only admin role, and a non-administrator
Windows account. Device-side command accounting (TACACS+ or
`archive log config`) then gives an independent record to compare with the
audit.

## Building the diagram, step by step

`scripts/main.py` turns the JSON files into `topology.drawio`:

1. **Parse.** `parser.py` loads every JSON file, and `netparse/` turns each
   device's raw output into structured data: hostname, interfaces (up/down,
   enabled, speed, MTU, MAC) and interface IPv4 addresses. Files in the older
   NAPALM-based format are still read as they are.
2. **Derive links.** Every interface address is grouped by subnet. A subnet
   shared by exactly two devices becomes a link between them. That's what a
   directly connected route means, so CDP and LLDP aren't needed. A subnet
   only one device has (an uplink to something not in the inventory) is
   skipped. A subnet three or more devices share is drawn as a full mesh,
   with a note in the log. Subnets in `config.EXCLUDED_NETWORKS` (such as a
   management network) are ignored. A link is up unless either end reports
   its interface as down.
3. **Place nodes.** Positions come from `template.drawio` at the project
   root (see [Node positions](#node-positions)). Devices that aren't in it
   yet are put in a staging grid below the rest.
4. **Route links.** `pathfinder.py` picks which side of each node every link
   leaves from and routes each link around the nodes so no two links overlap
   (see [Link routing](#link-routing)).
5. **Render the physical diagram.** `renderer.py` writes the nodes (with
   icons where set), the routed links and the interface labels.
6. **Run features.** Each enabled feature draws onto its own layer (see
   [Feature layers](#feature-layers)). The log reports `ok` or the error for
   each one.
7. **Write files.** It writes `output/topology.drawio`, then rewrites
   `template.drawio` with every node's final position.

## Rendering features

### Physical diagram (base layer)

The base layer is always drawn. Everything in it comes from the core show
commands.

#### Nodes and icons

Each device is a node labelled with its inventory name. If the host has an
`icon` in the inventory, the node shows that picture from
`docs/Prod_Library.xml`. The value must match an entry's title in the
library exactly, such as `"C9200-24P Front"`. A host with no icon is drawn
as a plain rounded rectangle. If an icon name isn't found, the log says so
and the rectangle is used.

#### Node positions

Node positions are kept in `template.drawio` at the project root, a
stripped-down diagram with one rectangle per device and nothing else.
To move nodes, open that file in draw.io, drag them, and save. The next run
uses the new positions. Each run rewrites the file to match the inventory:
existing nodes keep their positions, and new devices appear in the staging
grid, ready to be dragged into place. Every node label in it must match an
inventory host, so a typo stops the run instead of silently dropping a node.

To force the side of a node that its links attach to, add a custom property
named `side` with the value `top`, `bottom`, `left` or `right` to that node in
`template.drawio` (right-click > **Edit Data**). Every link of that node then
attaches to that side. Without one, the side is chosen per link: if the two
nodes' centres are within 40 pixels of each other vertically (the same row),
the link leaves the right side of the left-hand node and enters the left side
of the other. Otherwise it leaves the bottom of the upper node and enters the
top of the lower one.

#### Links and interface labels

Each link is drawn as an orthogonal line between the two devices. At each
end, a label shows the abbreviated interface name (`GigabitEthernet0/1`
becomes `Gi0/1`), placed against the node. The label background is green if
the link is up and red if either end reports it down. A device that reports
no status for an interface counts as up, so this is a display aid, not a
health check.

#### Link routing

`pathfinder.py` routes links on a Hanan grid: a sparse grid built from every
node edge, node centre and port position. It routes one link at a time with
A* search, and each placed link is marked on a shared congestion map, so
later links route around it. This guarantees that no two links run on top of
each other. Links bend only where they have to, and links heading the same
way tend to run together in parallel lanes. If a node side doesn't have room
for all its ports, the node is widened by exactly the amount needed.

These settings in `config.py` tune the routing:

| Setting | Effect |
| --- | --- |
| `HANAN_BUFFER` | Minimum gap between parallel links |
| `HANAN_TURN_PENALTY` | How strongly straight runs are preferred over extra bends |
| `HANAN_MARGIN` | How far ports are kept from a node's corners |
| `HANAN_AFFINITY_PENALTY` | How strongly links are pulled into running alongside each other |

`info.md` explains the routing algorithm in more depth.

### Feature layers

Everything drawn on top of the physical diagram comes from a feature: one
module in `scripts/features/`. Features are built to be added one at a time
without disturbing each other or the base diagram:

- **Own layer.** Each feature draws onto its own named draw.io layer, which
  viewers can show, hide or lock.
- **Own data.** Each feature declares the show commands it needs per
  platform. Collection picks them up automatically, so adding a feature never
  means editing the playbook.
- **Own copy of the topology.** Each feature gets a private copy of the
  devices, links and routed geometry, so it can't change what another
  feature sees.
- **All or nothing.** A feature's layer is added only if it finishes without
  an error. If it fails, the error is logged and that one layer is left out.
  The rest of the diagram is unaffected.
- **No id clashes.** Every cell a feature draws has an id prefixed with the
  feature name.
- **Own settings.** Each feature has a `SETTINGS` dictionary of defaults
  (colours, spacing), which can be overridden per feature in
  `config.FEATURE_SETTINGS`.

`config.ENABLED_FEATURES` lists the features to run. Its order is also the
layer stacking order.

A feature runs in three steps:

| Step | Input | Output |
| --- | --- | --- |
| `collect(topo)` | Each device's JSON, through `topo.cli(host, command)` | The feature's parsed data |
| `plan(data, topo)` | Parsed data plus the routed physical diagram | Geometry: what to draw and where |
| `render(plan, layer)` | The plan | Cells on the feature's layer |

`topo` gives each step the devices, links and node geometry, plus helpers
that most features need: which device owns an IP address, and the
fewest-hops path between two devices.

#### IPsec tunnels (`features/ipsec.py`)

**Layer:** "IPsec tunnels". **Command:** `show crypto session detail`
(IOS / IOS-XE).

What it shows: every IPsec tunnel between two inventory devices, drawn as a
curved, dashed line that passes over every router the tunnel crosses.
Tunnels are purple when up and red when either end reports them down.
Hovering a tunnel shows its endpoints, addresses, interfaces, status and
path.

How it works:

1. **Find the sessions.** Each device's `show crypto session detail` output
   is parsed into sessions: tunnel interface, local address, peer address
   and status. The local address comes from the IKE SA line, or from the
   IPsec flow if IKE is down.
2. **Find both ends.** Each peer address is looked up among every interface
   address on every device, loopbacks included. The device that owns it is
   the far end. A peer that isn't in the inventory is skipped.
3. **Merge.** Both ends usually report the same tunnel, so the two reports
   are merged into one tunnel. It's drawn as down if either end doesn't
   report it as up.
4. **Find the path.** The routers in between are found with a fewest-hops
   search over the physical links, preferring links that are up. This is the
   likely underlay path, not necessarily the one the routing table uses.
5. **Lay out the lines** (`features/bundling.py`):
   - Tunnels that end on the same device all approach it from the same side
     and run in evenly spaced parallel lanes for the last stretch.
   - Tunnels that share a physical hop run side by side along it, ordered so
     they don't cross, and fan out where their paths split.
   - Over a transit router, each tunnel keeps its lane, so they pass over in
     parallel.

Settings (`config.FEATURE_SETTINGS["ipsec"]`): `up_color`, `down_color`,
`style`, `lane_spacing`, `converge_near`, `converge_far`, `transit_spacing`.

If a device's tunnel data comes from somewhere other than this collector,
its JSON can carry the tunnels already parsed, each with an optional explicit
`path`, which then replaces the fewest-hops guess:

```json
"ipsec": {"tunnels": [{"interface": "Tunnel10", "local_address": "203.0.113.1",
                       "peer_address": "198.51.100.7", "status": "UP-ACTIVE",
                       "path": ["branch1", "isp-rtr", "hq"]}]}
```

#### Adding a feature

1. Create `scripts/features/<name>.py` with a subclass of `Feature`. Set
   `name`, `label` (the layer name), `commands` (for example
   `{"ios": ["show vrf detail"]}`) and `SETTINGS`. Implement `collect`, `plan`
   and `render`, and end the module with `FEATURE = MyFeature()`. Overlays
   that cross routers can reuse `features.bundling.plan_overlays`.
2. Add the name to `config.ENABLED_FEATURES`.
3. Add sample device output under `tests/fixtures/` and a
   `tests/test_<name>.py`.
4. Every command in `commands` must pass the platform's read-only policy.
   `tests/test_readonly.py` checks this automatically, for every feature
   whether it is enabled or not.

### Adding a device platform

Device platforms are separate from features. A platform tells collection
what to run and tells `netparse` how to read the core output. To add one
(NX-OS, EOS, PAN-OS and so on):

1. Write `scripts/netparse/<platform>.py` with `CORE_COMMANDS`, optionally
   `PAGING_COMMANDS` and `CONFIG_CHECK_COMMAND`, and `parse(cli)`, which
   returns `hostname`, `interfaces` and `interfaces_ip` in the same shape as
   `ios.py`.
2. Register it in `_DRIVERS` and `_ALIASES` in `scripts/netparse/__init__.py`.
3. Add a read-only policy for it to `scripts/readonly.py`. Without one, the
   collector refuses to contact the platform, and the tests fail.
4. For each feature that should work on the new platform, add its commands
   and a parser to that feature.

## Configuration reference

All settings live in `scripts/config.py`:

| Setting | Purpose |
| --- | --- |
| `DEFAULT_FACTS_DIR`, `DEFAULT_OUTPUT_DIR` | Where JSON is read from and the diagram is written to |
| `DEFAULT_INVENTORY_PATH`, `DEFAULT_TEMPLATE_PATH`, `DEFAULT_ICON_LIBRARY_PATH` | Inventory, position template and icon library |
| `EXCLUDED_NETWORKS` | Subnets never drawn as links, including any smaller subnet inside them |
| `NEW_NODE_GRID_*` | Layout of the staging grid for new devices |
| `DEFAULT_NODE_W`, `DEFAULT_NODE_H` | Node size when the inventory doesn't set `w` / `h` |
| `HANAN_*` | Link routing, see [Link routing](#link-routing) |
| `STATUS_UP_COLOR`, `STATUS_DOWN_COLOR` | Interface label colours |
| `INTF_LABEL_STYLE`, `INTF_ABBREVIATIONS` | Interface label style and name abbreviations |
| `ENABLED_FEATURES` | Features to run, in layer order |
| `FEATURE_SETTINGS` | Per-feature overrides, such as `{"ipsec": {"up_color": "#0050EF"}}` |

`main.py` also takes these on the command line:

```bash
python3 scripts/main.py [facts_dir] [output_dir] [--inventory=PATH] [--template=PATH] [--icon-library=PATH]
```

## Running it

**In AWX:** create a job template for `playbooks/gather_topology.yml` with
the project's `inventory.yml` as its inventory, and attach a Network
credential. When the job finishes, the diagram is at the path on the
artifact server that the last play copies it to, and the raw JSON is
alongside it under `output/facts/`. The stock AWX execution environment is
enough, since everything the job needs is standard Python and the OpenSSH
client.

**By hand:**

```bash
export NET_USERNAME=admin NET_PASSWORD=secret
ansible-playbook -i inventory.yml playbooks/gather_topology.yml
```

To rebuild the diagram from JSON that's already been collected, without
contacting any device:

```bash
python3 scripts/main.py output/facts output
```

Open `output/topology.drawio` in draw.io (desktop, web, or the VS Code
extension).

## The C extension

The A* search in `pathfinder.py` runs in a CPython C extension,
`scripts/csrc/_hanan.c`, about 80 times faster than the same code in Python.
It's built against CPython's stable ABI, so one compiled file,
`scripts/_hanan.abi3.so`, loads on every CPython 3.9 or later on Linux
x86_64, and needs nothing newer than glibc 2.4. That file is prebuilt and
committed, so AWX needs no compiler or Python headers.

If you change `_hanan.c`, rebuild the binary on a machine with gcc and the
Python headers, and commit it with the source:

```bash
python3 scripts/build_ext.py           # build
python3 scripts/build_ext.py --check   # does the committed binary match the source?
```

The build stamps the source's SHA-256 into the binary, and the tests fail if
the committed binary doesn't match the source. At runtime, a stale binary is
rebuilt if a compiler is available, and otherwise used with a warning. On a
platform other than Linux x86_64 (an ARM execution environment, say), run
`build_ext.py` there once. The pure-Python `astar_route_py` is kept as the
readable reference, and the tests check that both produce identical routes.

## Tests

```bash
python3 -m unittest discover -s tests
```

The fixtures in `tests/fixtures/` describe a six-router topology with three
IPsec tunnels. The tests cover the YAML parser, the IOS parsers, the SSH
session driver (against a fake `ssh`), the feature framework, the IPsec
feature, the C extension against the Python reference, a full run of
`main.py`, and the read-only safeguards. The fake `ssh` logs every line it
receives, so the read-only tests compare what the "device" got with the
audit record, and check that refused commands never reach it, that a
confirmation prompt is cancelled rather than answered, and that a
configuration change fails the host.

## Limitations and scaling

### Current limitations

- Only Cisco IOS / IOS-XE is supported today. The collector refuses to
  contact a device on any other platform, because there is no read-only
  policy for it yet. Palo Alto (direct and through Panorama) and Windows
  Server (PowerShell over OpenSSH) are planned in the
  [multi-vendor design](docs/design-archive/2026-10-26/multi-vendor-device-data-design.md).
- Links come from IPv4 subnets. A device with only unnumbered or IPv6
  interfaces, or a pure layer-2 switch with no routed interfaces, won't
  connect to anything.
- The design assumes point-to-point subnets (a /30 or /31 per link). Shared
  segments are drawn as a full mesh.
- The IPsec path is the fewest-hops guess unless an explicit path is given.
- Collection uses SSH and a CLI. Palo Alto will use the PAN-OS XML API, and
  Windows will use PowerShell over OpenSSH, as described in the multi-vendor
  design.

### Scaling to hundreds of devices

draw.io itself copes with diagrams this size. Cell counts in the low
thousands open and pan normally, and layers keep feature data out of the
way until it's wanted. Two parts of this project need work before a
production run of hundreds of devices:

- **Icon size.** Each icon in `docs/Prod_Library.xml` is 90 to 350 KB, and
  every node that uses one carries its own copy. Without icons, the diagram
  is about 2 KB per device, so 400 devices make an 0.8 MB file. With icons
  on all 400, the same diagram would be 60 to 140 MB, which draw.io handles
  badly. The fix is smaller icons (simplified SVGs of a few KB each) or
  referencing each image once instead of embedding it per node.
- **Routing time.** Routing time grows with the square of the number of
  links. Measured on synthetic grid topologies:

  | Devices | Links | Build time |
  | --- | --- | --- |
  | 50 | 85 | 1 s |
  | 100 | 179 | 3.5 s |
  | 200 | 371 | 13 s |
  | 400 | 759 | 52 s |

  That's fine for a scheduled AWX job, but 1,000 or more devices on one page
  would take several minutes.

Production is currently one site of a few hundred devices with no site
mapping, so for now the work is on keeping one large page fast and small
(the icon fix above). If draw.io's limits are reached, the way to scale is
to split the network across draw.io pages (the tabs along the bottom), one
per site, region or segment, plus an overview page with one node per site. Each page is routed separately, which keeps routing fast
and each page readable. Layers then hold the detail within a page: VLANs,
VRFs, tunnels, firewall zones.

If even that outgrows draw.io, the collected JSON doesn't depend on it.
Another renderer, such as an interactive web view with search and
filtering, could be added alongside draw.io and read the same files.

## Design history

Design documents are kept in `docs/design-archive/`, one folder per date,
so the reasoning behind each change stays readable after the code moves on.
A later revision gets a new dated folder rather than editing an older one.
See [docs/design-archive/README.md](docs/design-archive/README.md) for the
index.
