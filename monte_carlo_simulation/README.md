# monte_carlo_simulation

A small Python package that evaluates deviation plans through repeated
`duarouter` and SUMO simulations. For a fixed trip demand and road closure, it
reroutes demand with stochastic edge weights, applies each candidate detour,
simulates the baseline and every plan and aggregates performance metrics.

## What it does

Each Monte Carlo iteration runs four stages:

1. **Stochastic routing** (`sumo_runner.py`) - runs `duarouter` on the same
   `trips.xml` with `--weights.random-factor`, producing a different baseline
   `routes.xml` for each run.
2. **Detour rewriting** (`simulation.py`) - applies each deviation plan to that
   run's baseline routes using `deviation_plan_maker.route_rewriter`.
3. **SUMO simulation** (`sumo_runner.py`) - runs SUMO once for the baseline and
   once for every plan-specific route file, then parses `tripinfo.xml`.
4. **Summary aggregation** (`summary.py`) - aggregates per-run metrics into
   descriptive statistics and writes `summary.json`.

Output files are written under the directory passed to `--output`:

- `run_<N>/routes.xml` - baseline route file produced by `duarouter`.
- `run_<N>/routes_<plan_id>.xml` - detoured route file for a candidate plan.
- `run_<N>/baseline/tripinfo.xml` - SUMO trip statistics for the baseline.
- `run_<N>/<plan_id>/tripinfo.xml` - SUMO trip statistics for a plan.
- `run_<N>/results.json` - per-plan metrics for that run.
- `summary.json` - aggregate statistics for baseline and plans.

The main source of Monte Carlo variation is stochastic routing through
`duarouter`, SUMO behavioral randomness is not the only variance source.

## Layout

```text
monte_carlo_simulation/
├── __init__.py
├── simulation.py   # CLI entry point and per-iteration orchestration
├── sumo_runner.py  # duarouter runner, SUMO config writer, tripinfo parser
├── trip_utils.py   # time windows, edge endpoints, rerouting counts
└── summary.py      # per-plan descriptive statistics aggregation
```

## Usage

First generate demand with `scenario_generator`:

```bash
python -m scenario_generator.generator data/configs/city_tiny_very_high/config.toml
```

Then run the Monte Carlo stage:

```bash
python -m monte_carlo_simulation.simulation \
    --net data/configs/city_tiny_very_high/city_tiny.net.xml \
    --plan data/detour_plans/detour_city_tiny.json \
    --trips output_city_tiny_very_high/trips.xml \
    --runs 30 \
    --parallel 4 \
    --random-factor 1.2 \
    --output mc_output/
```

Useful options:

- `--runs` - total number of runs to produce.
- `--parallel` - worker count, `0` uses all CPUs.
- `--resume` - reuse existing completed `run_<N>/results.json` files and
  create only missing runs up to `--runs`.
- `--random-factor` - upper bound for `duarouter --weights.random-factor`.
- `--begin`, `--end` - optional simulation window override in seconds.

Plan JSON files are produced by `deviation_plan_maker` and use this shape:

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
      "total_length_m": 1240.5
    }
  ]
}
```

`source_node`, `target_node`, `depth`, `rank` and `total_length_m` are optional
for legacy files. For upstream-aware plans, keep them in the JSON so the
rewriter can preserve depth-aware plan selection.

## Dependencies

- Python 3.11+
- `sumo` and `duarouter` on `PATH`
- `deviation_plan_maker` as a sibling package
