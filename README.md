# An Experimental Framework for Evaluating Traffic Deviation Plans

This repository contains the code produced for my Master's thesis in Computer Sciences. The thesis investigates how a microscopic traffic digital twin, combined with a Monte Carlo simulation protocol, can be used to systematically evaluate and rank candidate deviation plans for urban road closures.

## Context

When a road segment must be closed (maintenance, events, accidents), traffic managers need to choose a deviation plan (a set of alternative routes to redirect affected vehicles). This framework automates that decision process end to end: it generates a realistic synthetic traffic scenario, enumerates candidate deviation plans, stress-tests each plan across many stochastic simulation runs, and produces a statistically grounded ranking.

## Repository structure

```
.
├── scenario_generator/       # Stage 1 — synthetic demand generation
├── deviation_plan_maker/         # Stage 2 — graph-based deviation plan computation
├── TBD monte_carlo_simulation/   # Stage 3 — SUMO simulation with random seeds
├── TBD evaluator/                # Stage 4 — statistical analysis and plan ranking
│
├── data/
│   ├── networks/             # SUMO road networks (.net.xml)
│   ├── configs/              # TOML configuration files per scenario
│   └── detour_plans/         # Pre-computed detour plan definitions (.json)
│
├── results/                  # Monte Carlo output folders (one per experiment)
├── run_pipeline.py           # End-to-end pipeline runner
│
└── report/                   # LaTeX source of the thesis report
```

## Pipeline

The four modules correspond to four sequential stages:

| Stage | Module | Input | Output |
|---|---|---|---|
| 1 | `scenario_generator` | `.net.xml`, `taz.xml`, `config.toml` | `routes.xml`, OD matrices |
| 2 | `deviation_plan_maker` | `.net.xml`, closed edge id | `plans.json` |
| 3 | `monte_carlo_simulation` | `routes.xml`, `plans.json` | `run_*/`, `summary.json` |
| 4 | `evaluator` | `summary.json` | `evaluation.json`, Markdown report |

Each module can also be used standalone — see the `README.md` inside each module directory for a detailed description.

## Modules

### `scenario_generator`

Generates a reproducible synthetic traffic demand scenario from a SUMO road network and a Traffic Analysis Zone (TAZ) file. The pipeline runs in three stages: (1) hourly OD matrices from a two-peak gravity model with an optional commute overlay, (2) a candidate route library per OD pair using Yen's *k*-shortest paths algorithm on an edge-expanded graph, and (3) vehicle instantiation with uniform departure times and route selection.

```bash
python -m scenario_generator.generator data/configs/city_tiny_very_high/config.toml
```

### `deviation_plan_maker`

Computes candidate deviation plans around a set of closed edges and rewrites a SUMO `routes.xml` to redirect affected vehicles. The module works in two stages.

**Stage 1 — detour planning** (`network_graph.py`): loads a SUMO `net.xml` into a lightweight directed graph (no external library) and finds alternative routes using two complementary strategies:

- `compute_deviation_plans` — Yen's k-shortest loopless paths from the closure's immediate source junction. All plans are at depth 0 and are sorted by routing cost. Use this when a single branching point at the closure entrance is sufficient.
- `compute_upstream_deviation_plans` — reverse BFS from the source junction up to a configurable depth, followed by Dijkstra from each discovered upstream origin. Plans are depth-tagged and sorted deepest-first, so the rewriter can intercept vehicles as far upstream as possible and fall back gracefully toward the depth-0 universal plan.

Before any path search, both methods resolve the detour endpoints: if the source junction has only one outgoing edge (no real alternative), the algorithm walks upstream until a proper branching point is found; the same adjustment is applied downstream at the target. Any such adjustments are recorded in `DeviationPlan.notes`.

Edge weights default to geometric length in metres. Free-flow travel time or any other scalar field can be loaded from a SUMO `edgedata.xml` file (`load_edge_weights_from_edgedata` with `invert=True` to convert speed to travel-time cost).

**Stage 2 — route rewriting** (`route_rewriter.py`): for every `<vehicle>`, `<trip>`, and `<flow>` in the routes file whose edge sequence crosses the closed segment, `apply_detour_to_routes` selects the deepest applicable plan (whose `source_node` appears in the vehicle's route before the closure) and splices the detour in. The depth-0 plan acts as the guaranteed fallback for every affected vehicle.

```python
from pathlib import Path
from deviation_plan_maker import NetworkGraph, apply_detour_to_routes

graph = NetworkGraph.from_net_xml(Path("network.net.xml"))
graph.load_edge_weights_from_edgedata(Path("edgedata.xml"), field="speed", invert=True)

plans, missing, blocked = graph.compute_upstream_deviation_plans(
    ["closed_edge_1"], depth=2
)

apply_detour_to_routes(
    input_routes=Path("routes.xml"),
    output_routes=Path("routes_detour.xml"),
    plans=plans,
    closed_edges=["closed_edge_1"],
    edge_nodes=graph.get_edge_nodes(),
)
```

A browser-based visualiser (`deviation_plan_maker/ui/index.html`) lets you load a `net.xml`, click edges to define a closure, and inspect the resulting detour overlaid on the network — no server required.

## Getting started

### 1. Prerequisites

- **Python 3.11+** (`tomllib` is part of the standard library from 3.11)
- **[SUMO](https://sumo.dlr.de/docs/Installing/)** — the `sumo` binary and `sumolib` must be reachable. After installing SUMO, set the two environment variables below (adjust the path to your actual installation):

```bash
export SUMO_HOME="/usr/share/sumo"          # or wherever SUMO is installed
export PYTHONPATH="$SUMO_HOME/tools:$PYTHONPATH"
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 3. Run each module standalone

If you only need one stage, each module exposes its own entry point.

**Stage 1 — Generate a traffic scenario**

```bash
python -m scenario_generator.generator data/configs/city_tiny_very_high/config.toml
```

Output (routes, OD matrices, plots) is written to the directory set by `output_path` in the config file.

**Stage 2 — Compute deviation plans**

```python
from pathlib import Path
from deviation_plan_maker import NetworkGraph, apply_detour_to_routes

graph = NetworkGraph.from_net_xml(Path("data/networks/city_tiny.net.xml"))
plans, _, _ = graph.compute_upstream_deviation_plans(["closed_edge_id"], depth=2)
apply_detour_to_routes(
    input_routes=Path("routes.xml"),
    output_routes=Path("routes_detour.xml"),
    plans=plans,
    closed_edges=["closed_edge_id"],
    edge_nodes=graph.get_edge_nodes(),
)
```

See `deviation_plan_maker/README.md` for the full API reference.

## Data

Network files and configuration files are provided in `data/`. The main test network is `city_tiny.net.xml`, a small synthetic urban network used for all experiments reported in the thesis. Larger networks (`cologne_reduced.net.xml`) are included for scalability experiments.

Configuration files (`.toml`) control all parameters of the demand model, simulation, and evaluation. The key parameters are documented inline and summarised in the thesis methodology chapter.

## Thesis report

The LaTeX source of the thesis is in `master_thesis_REPORT/`. To compile:

```bash
cd master_thesis_REPORT
latexmk -pdf main.tex
```

Requires a TeX Live distribution with `biblatex` and `biber`.

---

*Hugo Callens — Master's thesis in Computer Sciences, 2026*
