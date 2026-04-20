# An Experimental Framework for Evaluating Traffic Deviation Plans

This repository contains the code produced for my Master's thesis in Computer Sciences. The thesis investigates how a microscopic traffic digital twin, combined with a Monte Carlo simulation protocol, can be used to systematically evaluate and rank candidate deviation plans for urban road closures.

## Context

When a road segment must be closed (maintenance, events, accidents), traffic managers need to choose a deviation plan (a set of alternative routes to redirect affected vehicles). This framework automates that decision process end to end: it generates a realistic synthetic traffic scenario, enumerates candidate deviation plans, stress-tests each plan across many stochastic simulation runs, and produces a statistically grounded ranking.

## Repository structure

```
.
├── scenario_generator/       # Stage 1 — synthetic demand generation
├── TBD deviation_plan_maker/     # Stage 2 — graph-based deviation plan computation
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

Each module can also be used standalone — see the `README.md` inside `scenario_generator/` for a detailed example.

## Modules

### `scenario_generator`

Generates a reproducible synthetic traffic demand scenario from a SUMO road network and a Traffic Analysis Zone (TAZ) file. The pipeline runs in three stages: (1) hourly OD matrices from a two-peak gravity model with an optional commute overlay, (2) a candidate route library per OD pair using Yen's *k*-shortest paths algorithm on an edge-expanded graph, and (3) vehicle instantiation with uniform departure times and route selection.

```bash
python -m scenario_generator.generator data/configs/city_tiny_very_high/config.toml
```

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
