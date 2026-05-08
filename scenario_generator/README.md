# scenario_generator

A small Python package that turns a SUMO road network and a TAZ (Traffic
Analysis Zone) file into a trips-based demand scenario: hourly OD matrices,
cached feasible route candidates and a SUMO `trips.xml` file.

## What it does

The pipeline runs in three stages:

1. **Demand generation** (`od_model.py`) - builds 24 hourly OD matrices, either
   from a two-peak gravity model with an optional commute overlay, or loaded
   from an external pickle.
2. **Route library** (`route_library.py`) - computes k shortest candidate
   paths per active OD pair on an edge-expanded graph of the network, using
   Yen's algorithm. Results are cached and used to choose feasible trip
   endpoint edges.
3. **Trip sampling** (`vehicle_sampler.py`) - assigns every trip a
   uniform-random departure time within its hour and exports only `from`/`to`
   endpoint edges. Full route assignment is left to `duarouter` in the Monte
   Carlo stage.

Output files are written under the directory set by `output_path` in the
config:

- `trips.xml` - SUMO trip definitions with `depart`, `from` and  `to` edges.
- `out_od.xml` - TAZ-level demand as SUMO `tazRelation` intervals.
- `od_matrices.pkl` - hourly OD matrices as a Python pickle.
- `trans_mat_definition/odmat_hour*.csv` - one CSV per hour for inspection.
- `cache/candidate_routes_<taz>.pkl` - cached feasible candidate routes.

## Layout

```text
scenario_generator/
├── generator.py          # CLI entry point
├── traffic_generator.py  # pipeline orchestrator
├── config_manager.py     # TOML config loader with path resolution
├── zones.py              # TAZ parsing and zone-level edge sampling
├── od_model.py           # gravity model, commute overlay, temporal profile
├── route_library.py      # edge-expanded routing graph and k-shortest paths
├── vehicle_sampler.py    # trip-by-trip departure time and endpoint selection
├── exports.py            # trips.xml, OD XML and  CSV writers
├── visualize.py          # matplotlib helpers for generated scenario data
└── LINEAGE_AND_RATIONALE.md
```

## Usage

```bash
python -m scenario_generator.generator data/configs/city_tiny_very_high/config.toml
```

The config is plain TOML. Known input paths such as `net_path`, `taz.path` and
`od_matrices` are resolved against the current directory and the config file's
parent directories. The output directory is the `output_path` value from the
config.

Important config fields:

- `net_path` - SUMO network file.
- `output_path` - destination directory for generated files.
- `sim_begin_time`, `sim_end_time` - currently expected to cover one full day,
  from `0` to `86400`.
- `data_aggr_frequency` - currently expected to be `3600`.
- `max_num_vehicles` or `synthetic_od.daily_total` - total synthetic demand.
- `random_seed` - shared seed for reproducible sampling.
- `candidate_routes.k_paths` - number of candidate paths retained per OD pair.
- `taz.path` - SUMO TAZ file.
- `commute_pattern` - optional directional AM/PM commute overlay.

## Dependencies

- Python 3.11+
- `sumolib` from the SUMO distribution
- `numpy`, `pandas`, `networkx`, `matplotlib`
