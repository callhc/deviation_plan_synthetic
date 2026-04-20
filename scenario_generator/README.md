# scenario_generator

A small Python package that turns a SUMO road network and a TAZ (Traffic Analysis Zone) file into a ready-to-simulate demand scenario: hourly OD matrices, candidate routes, and a SUMO `routes.xml` file.

## What it does

The pipeline runs in three stages:

1. **Demand generation** (`od_model.py`) — builds 24 hourly OD matrices, either from a two-peak gravity model with an optional commute overlay, or loaded from an external pickle.
2. **Route library** (`route_library.py`) — computes *k* shortest candidate routes per active OD pair on an edge-expanded graph of the network, using Yen's algorithm. Results are cached.
3. **Vehicle sampling** (`vehicle_sampler.py`) — assigns every trip a uniform-random departure time within its hour and picks one route uniformly from the candidate set.

Output files are written under the directory set by `output_path` in the config:

- `routes.xml` — SUMO vehicle definitions with inline routes
- `out_od.xml` — TAZ-level demand as SUMO `tazRelation` intervals
- `od_matrices.pkl` — hourly OD matrices as a Python pickle
- `trans_mat_definition/odmat_hour*.csv` — one CSV per hour for inspection
- `plots/` — PDF plots of temporal/spatial demand and route statistics

## Layout

```
scenario_generator/
├── generator.py          # CLI entry point
├── traffic_generator.py  # pipeline orchestrator
├── config_manager.py     # TOML config loader with path resolution
├── zones.py              # TAZ parsing and zone-level edge sampling
├── od_model.py           # gravity model + commute overlay + temporal profile
├── route_library.py      # edge-expanded routing graph + k-shortest paths
├── vehicle_sampler.py    # trip-by-trip departure time + route selection
├── exports.py            # SUMO / CSV writers
└── visualize.py          # matplotlib plots for the generated scenario
```

## Usage

```bash
python -m scenario_generator.generator path/to/config.toml
```

The config is plain TOML; all filesystem paths it contains (network, TAZ file, optional external OD pickle) are resolved relative to the config file itself.

## Dependencies

- Python 3.11+
- `sumolib` (from the SUMO distribution)
- `numpy`, `pandas`, `networkx`, `matplotlib`
