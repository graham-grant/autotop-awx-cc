Here is an architectural overview of how your automated topology drawing script works, focusing on the mechanics of the Hanan-grid A* routing engine and how the entire end-to-end pipeline ties together.

---

### Part 1: How the Hanan-Grid A* Routing Works

The pathfinding system in `pathfinder.py` is designed to solve a hard problem: finding clean, non-overlapping, orthogonal paths for network cables around rectangular network devices without clipping corners or cutting through nodes. It does this using a combination of **Hanan Grids** and the **A* search algorithm**.

```
         Node A
     +------------+
     |            |
=====*============*===== <-- Hanan Grid Line (Node Edges)
     |            |
     +------------+
           |
           | <-- Hanan Grid Line (Port Coordinate)

```

#### 1. Constructing the Hanan Grid

Instead of searching a massive, pixel-by-pixel map (which would be extremely slow), the router creates an intelligent, sparse search space called a **Hanan Grid**.

* **Key Concept:** Geometrist Maurice Hanan proved that the shortest orthogonal path between points amidst rectangular obstacles can always be found using *only* grid lines aligned with the boundaries of those obstacles and terminals.
* **Building the Grid:** The engine collects all $X$ and $Y$ coordinates from every node's edges, every node's exact center axis, and every dynamic port point. These coordinates form a custom, non-uniform grid.
* **Densification:** To allow "multi-lane highways" in wide-open corridors (so parallel cables don't crowd each other out), the grid is densified by injecting extra parallel grid lines spaced out at a safe `buffer` distance.

#### 2. Defining Obstacles and State Space

* **Blocking Nodes:** Any vertex or grid segment that falls strictly *inside* a node's bounding rectangle is flagged as blocked.
* **Carving Exceptions:** When routing a specific link, a temporary exception is carved into the map to allow travel through the exact port coordinates chosen on the source and target node boundaries.
* **3D State Graph:** The A* search doesn't just evaluate `(x, y)` locations. It searches across a 3D state network defined by `(x_index, y_index, incoming_direction)`. Tracking the direction allows the algorithm to look ahead and apply a **turn penalty** to discourage unnecessary zig-zags, ensuring clean, straight cable lines.

#### 3. Execution, Spacing, and Congestion

* **A* Heuristic:** The search uses standard Manhattan distance ($|x_1 - x_2| + |y_1 - y_2|$) as its cost estimator, allowing it to navigate towards the destination rapidly.
* **One-by-One Routing & Stamping:** Links are routed sequentially. Once a link finds its optimal path, it "stamps" its footprint onto a global congestion map. Subsequent links see those grid segments as penalized or blocked, forcing them to wrap around or take an outer lane, preventing overlapping overlapping lines.

---

### Part 2: Overview of the Complete Pipeline

The collection of scripts works as a modular, data-driven automation pipeline that moves from live network state straight to a fully stylized visual graph.

```
 [ Device JSON  ] ──> ( parser.py ) ──> [ Edge/Node Dicts ]
                                              │
 [ template.drawio ] ──> ( main.py ) <────────┘
        │                     │
        ▼                     ▼
 (pathfinder.py) ──────> (renderer.py) ──> [ topology.drawio ]

```

#### 1. Data Ingestion (`parser.py`)

* Reads structured `*.json` configuration and state telemetry dumps captured by the Ansible playbook (raw show-command output, parsed by `netparse/`).
* Groups interface IPv4 addresses by their calculated subnets. If exactly two inventory hosts share a subnet, it deduces a point-to-point physical link and notes whether the interfaces are operationally UP or DOWN.

#### 2. Layout & Coordination (`main.py`)

* Acts as the traffic cop for the workflow.
* It opens a clean template file (`template.drawio`) which acts as a physical canvas. You can open `template.drawio` in Draw.io, drag your router/switch rectangles wherever you visually want them, and save. `main.py` extracts those exact layout positions ($X, Y$ coordinates and size).
* It also reads custom styling attributes attached to those template nodes, such as a user-preferred override side for cabling (e.g., forcing all WAN links out of the `top` of an edge router).

#### 3. Mathematical Path Planning (`pathfinder.py`)

* Takes the nodes, the physical dimensions, and the list of inter-device links.
* Determines which sides of the nodes the cables should attach to using a majority-vote angle calculation (grouping links logically to face their neighbors).
* Evaluates link density per face: if a node face has only a single link, it constrains the layout to the exact geometric center.
* Runs the Hanan-Grid A* router to calculate the definitive string of polyline waypoints for every cable segment.

#### 4. Vector Rendering Pass (`renderer.py`)

* Takes the raw polyline points and builds a native Draw.io XML map (`topology.drawio`) using exit/entry constraint definitions so the wires remain physically pinned to the node edges if moved.
* Layers on cosmetic enhancements: it calculates relative offset positions near the node boundaries to insert high-contrast IP address boxes and dynamically rotated interface label flags (`Gi0/1`, `Eth1`).
* Uses Draw.io's `labelBackgroundColor` attribute to inject the link state color straight behind the text box (green for operational links, red for down interfaces) while stripping away old floating dots to yield a highly readable diagram.

Transitioning from fixed, pre-assigned port coordinates to a **range of dynamic options** shifts the behavior of the routing engine in several ways. It changes the optimization problem from a simple point-to-point path search into a joint optimization of port selection and wire routing.

### 1. Multi-Goal Optimization (Expanding the Search Frontier)

In a traditional A* setup with a fixed source and destination interface, the search frontier begins at a single `(X, Y)` coordinate and moves toward a single target coordinate.

By introducing a list of multiple valid port points (`start_pts` and `goal_pts`), the algorithm's behavior changes:

* **The Starting Frontier:** Instead of pushing a single starting node onto the A* priority queue, **all valid source ports are pushed onto the queue simultaneously** with an initial path cost of `0`.
* **The Goal Target:** A* evaluates success not by matching a single precise point, but by asking, *"Has my path touched any coordinate contained within the `goal_pts` set?"*

The pathfinder explores multiple candidate paths out of multiple interfaces in parallel. The path that reaches *any* valid destination interface with the lowest overall cost (shortest distance + fewest turns + lowest congestion penalty) wins.

### 2. Elimination of Self-Crossing and "Loop-Backs"

When interfaces are fixed in advance (for example, blindly choosing the top-left edge for Interface 1 and the top-right edge for Interface 2), a link may be forced to cross over its own neighbor immediately upon exiting the device to get to its destination.

With a range of options, the A* algorithm inherently resolves this. If using the leftmost available grid line allows a wire to go straight to its destination without turning or crossing, A* will naturally select that port because it incurs the lowest path cost. Wires will spontaneously untangle themselves and sequence nicely side-by-side along the node's face.

### 3. Dynamic "Stamping" and Spillover Behavior

Because links are routed sequentially one at a time, each path stamps a congestion penalty into the grid when it completes.

* **With fixed ports:** If a corridor becomes heavily congested or blocked, a later link with a fixed port is forced to take massive, awkward detours around the blockages to reach its mandatory coordinate.
* **With a range of options:** If the ideal, shortest-path port is surrounded by grid segments heavily penalized by previous links, the A* pathfinder might find that it is "cheaper" to shift over and use an adjacent, unpenalized port coordinate instead. Wires will dynamically slide down the face of the node to avoid congestion.

### 4. Why Centralizing Solitary Links is Necessary

The primary side-effect of giving A* complete freedom over a range of points is that **it is entirely indifferent to symmetry.** It cares only about minimizing total cost.

If a device only has a single link on a given face, and its destination node is shifted slightly to the left, the absolute shortest path is a straight vertical line. A* will naturally select a port shifted to the left edge of the node to shave off a few pixels of horizontal distance.

By hardcoding the condition we added earlier—detecting when the face density equals `1` and forcing `[valid[0]]` to be the exact midpoint—you intentionally override this efficiency. You force the search frontier to collapse back down to a single option, trading a minor amount of path efficiency for visual balance and alignment.