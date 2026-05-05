# deviation_plan_maker

A small Python package that computes detour plans around road closures in a SUMO network and rewrites existing route files to apply those plans to affected vehicles.

## What it does

The package works in two stages:

1. **Detour planning** (`network_graph.py`) — loads a SUMO `net.xml` file into a lightweight directed graph and finds alternative routes around a set of closed edges. Two strategies are available:
   - `compute_deviation_plans` — Yen's k-shortest loopless paths from the closure's immediate source junction (depth-0 plans).
   - `compute_upstream_deviation_plans` — reverse BFS from the source junction up to a configurable depth, followed by Dijkstra from each discovered upstream origin. Produces depth-tagged plans so the rewriter can match each vehicle at the earliest upstream branching point.
2. **Route rewriting** (`route_rewriter.py`) — reads a SUMO `routes.xml` and, for every vehicle whose route crosses the closed segment, splices in the deepest applicable detour plan. The depth-0 plan is always present as a universal fallback for vehicles that branch off only at the closure itself.

Edge weights default to geometric edge length (metres); free-flow travel times or any other scalar field can be loaded from a SUMO `edgedata.xml` file.

## Layout

```
deviation_plan_maker/
├── deviation_plan.py   # shared data classes: PathResult, DeviationPlan
├── network_graph.py    # graph construction, Dijkstra, Yen's, upstream BFS
├── route_rewriter.py   # depth-aware detour splicing into routes.xml
└── ui/                 # browser-based network visualiser (standalone HTML/JS)
    ├── index.html
    ├── app.js
    ├── network.js
    └── style.css
```

## Usage

```python
from pathlib import Path
from deviation_plan_maker import NetworkGraph, apply_detour_to_routes

graph = NetworkGraph.from_net_xml(Path("network.net.xml"))

# Optional: load travel-time weights
graph.load_edge_weights_from_edgedata(
    Path("edgedata.xml"), field="speed", invert=True
)

# Compute detour plans (depth-0 only)
plans, missing, blocked = graph.compute_deviation_plans(
    ["closed_edge_1", "closed_edge_2"], k=3
)

# Or compute upstream-aware plans
plans, missing, blocked = graph.compute_upstream_deviation_plans(
    ["closed_edge_1"], depth=2
)

# Apply plans to a SUMO routes file
apply_detour_to_routes(
    input_routes=Path("routes.xml"),
    output_routes=Path("routes_detour.xml"),
    plans=plans,
    closed_edges=["closed_edge_1", "closed_edge_2"],
    edge_nodes=graph.get_edge_nodes(),
)
```

The browser UI (`ui/index.html`) can be opened directly in any modern browser. Load a `net.xml` file, click edges to select a closure, and inspect the resulting detour plan overlaid on the network.

## Dependencies

- Python 3.11+
- Standard library only (`xml.etree.ElementTree`, `heapq`, `logging`)
- No external graph library required
