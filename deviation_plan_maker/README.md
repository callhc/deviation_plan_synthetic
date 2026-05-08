# deviation_plan_maker

A small Python package that computes candidate detour plans around closed SUMO
edges and can rewrite routed SUMO demand so affected vehicles use those plans.
****
## What it does

The package works in two stages:

1. **Detour planning** (`network_graph.py`) - loads a SUMO `net.xml` file into
   a lightweight directed graph and finds alternatives around one or more
   closed edges. `compute_deviation_plans` computes depth-0 Yen k-shortest
   loopless paths from the closure's immediate upstream junction.
   `compute_upstream_deviation_plans` walks upstream with reverse BFS,
   computes detours from discovered origins and tags plans by depth.
2. **Route rewriting** (`route_rewriter.py`) - reads a SUMO `routes.xml` file
   and, for each vehicle whose route crosses the closed segment, splices in the
   deepest applicable detour plan. A depth-0 plan is required as the universal
   fallback.

Output objects and files:

- `DeviationPlan` objects containing detour edges, source/target junctions,
  depth, rank, total length and diagnostic notes.
- JSON-compatible plan payloads for `monte_carlo_simulation`.
- Rewritten `routes.xml` files when `apply_detour_to_routes` is used.

Edge weights default to geometric length. `load_edge_weights_from_edgedata`
can replace them with another scalar field, for example inverted speed as a
travel-time proxy.

## Layout

```text
deviation_plan_maker/
├── __init__.py          # public package exports
├── deviation_plan.py    # PathResult and DeviationPlan data classes
├── network_graph.py     # graph construction, Dijkstra, Yen, upstream BFS
├── route_rewriter.py    # depth-aware detour splicing into routes.xml
└── ui/                  # standalone browser visualiser
    ├── index.html
    ├── app.js
    ├── network.js
    └── style.css
```

## Usage

```python
from pathlib import Path
from deviation_plan_maker import NetworkGraph, apply_detour_to_routes

graph = NetworkGraph.from_net_xml(Path("data/networks/city_tiny.net.xml"))

# Optional: load alternative routing weights.
graph.load_edge_weights_from_edgedata(
    Path("edge_weights.xml"),
    field="speed",
    invert=True,
)

# Depth-0 alternatives from the closure entrance.
plans, missing, blocked = graph.compute_deviation_plans(
    ["closed_edge_1"],
    k=3,
)

# Or upstream-aware alternatives.
plans, missing, blocked = graph.compute_upstream_deviation_plans(
    ["closed_edge_1"],
    depth=2,
)

apply_detour_to_routes(
    input_routes=Path("routes.xml"),
    output_routes=Path("routes_detour.xml"),
    plans=plans,
    closed_edges=["closed_edge_1"],
    edge_nodes=graph.get_edge_nodes(),
)
```

Plan JSON files consumed by `monte_carlo_simulation` use this shape:

```json
{
  "closed_edge": "edge_id",
  "plans": [
    {
      "plan_id": "plan_1",
      "detour_edges": ["edge_a", "edge_b"],
      "source_node": "node_a",
      "target_node": "node_b",
      "depth": 0,
      "rank": 1,
      "total_length_m": 1250.0
    }
  ]
}
```

`source_node`, `target_node`, `depth`, `rank` and `total_length_m` are
recommended because they preserve upstream-aware rerouting behavior. Legacy
plans without `depth` are treated as depth-0 plans.

The browser UI can be opened directly from `ui/index.html`. It loads a SUMO
network, lets you select a closed edge visually, previews a candidate detour,
and exports a JSON file compatible with the Monte Carlo stage.

## Dependencies

- Python 3.11+
- Python standard library only for the package code
- No external graph library required
