# An Experimental Framework for Evaluating Traffic Deviation Plans

This repository contains the code produced for a Master's thesis in Computer
Sciences. It studies how a microscopic SUMO traffic digital twin, stochastic
routing, and Monte Carlo analysis can be combined to compare candidate
deviation plans for road closures.

When an edge has to be closed for works, events, or incidents, the framework
supports the full analysis workflow:

1. Generate a synthetic one-day traffic demand scenario.
2. Build candidate deviation plans around the closure.
3. Run repeated SUMO simulations with stochastic route choice.
4. Evaluate and rank the plans with paired statistics and multi-criteria
   decision analysis.

The stages are intentionally usable as independent Python packages. There is
currently no single end-to-end runner in the repository; run the stages you
need explicitly.

## Repository Structure

```text
.
├── scenario_generator/       # Stage 1: synthetic OD demand and trips.xml
├── deviation_plan_maker/     # Stage 2: detour computation and route rewriting
├── monte_carlo_simulation/   # Stage 3: repeated duarouter + SUMO runs
├── evaluator/                # Stage 4: statistics, MCDA, and reports
├── data/
│   ├── configs/              # Example TOML scenario configs and local inputs
│   ├── detour_plans/         # Example deviation-plan JSON files
│   └── networks/             # SUMO .net.xml networks
├── report/                   # LaTeX thesis report source and compiled PDF
├── requirements.txt          # Python dependencies, excluding SUMO/sumolib
└── mc_pipeline_diagram.svg   # Pipeline diagram used by the thesis material
```

## Pipeline

| Stage | Package | Main input | Main output |
|---|---|---|---|
| 1 | [`scenario_generator`](scenario_generator/README.md) | SUMO `net.xml`, TAZ file, TOML config | `trips.xml`, OD matrices, inspection CSVs |
| 2 | [`deviation_plan_maker`](deviation_plan_maker/README.md) | SUMO `net.xml`, closed edge ids | `DeviationPlan` objects or JSON plan files |
| 3 | [`monte_carlo_simulation`](monte_carlo_simulation/README.md) | `trips.xml`, plan JSON, SUMO network | `run_*/results.json`, `summary.json` |
| 4 | [`evaluator`](evaluator/README.md) | Monte Carlo output directory | `evaluation.json`, `evaluation.md`, best-plan ranking |

## Setup

Use Python 3.11 or newer. `tomllib` is part of the standard library from 3.11.

Install SUMO separately and make both the command-line binaries and `sumolib`
available:

```bash
export SUMO_HOME="/usr/share/sumo"
export PATH="$SUMO_HOME/bin:$PATH"
export PYTHONPATH="$SUMO_HOME/tools:$PYTHONPATH"
```

Then install the Python dependencies:

```bash
pip install -r requirements.txt
```

`requirements.txt` intentionally does not list `sumolib`, because it is bundled
with SUMO.

## Basic Usage

Generate a demand scenario:

```bash
python -m scenario_generator.generator data/configs/city_tiny_very_high/config.toml
```

The generator writes its files to the `output_path` configured in the TOML file,
including `trips.xml`.

Build or export a deviation-plan JSON file with `deviation_plan_maker`. The
package exposes a Python API and a browser-only visual interface at
`deviation_plan_maker/ui/index.html`; see
[`deviation_plan_maker/README.md`](deviation_plan_maker/README.md) for details.

Run Monte Carlo simulations:

```bash
python -m monte_carlo_simulation.simulation \
    --net data/configs/city_tiny_very_high/city_tiny.net.xml \
    --plan data/detour_plans/detour_city_tiny.json \
    --trips output_city_tiny_very_high/trips.xml \
    --runs 30 \
    --parallel 4 \
    --output mc_output/
```

Evaluate the completed runs:

```bash
python -m evaluator.evaluate --input mc_output/
```

The evaluator writes `mc_output/evaluation.json` and `mc_output/evaluation.md`.

## Data

The `data/` directory contains small networks, example scenario configs, and
example deviation plans used for experimentation. The `city_tiny` variants are
the main compact test cases. `cologne_reduced.net.xml` is included for larger
network experiments.

Configuration files control scenario generation parameters such as demand
volume, TAZ input, temporal profile, random seed, and commute-pattern overlays.
The package READMEs document the file contracts between stages.

## Thesis Report

The LaTeX source and compiled PDF are in `report/`. To rebuild the report:

```bash
cd report
latexmk -pdf main.tex
```

This requires a TeX distribution with `biblatex` and `biber`.

---

Hugo Callens - Master's thesis in Computer Sciences, 2026
