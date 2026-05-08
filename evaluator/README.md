# evaluator

A small Python package that analyses the output of `monte_carlo_simulation` and
produces a ranked recommendation for the best deviation plan.

## What it does

The evaluation pipeline runs in four stages:

1. **Statistical analysis** (`metrics.py`) - computes paired
   plan-minus-baseline deltas for delay and mean travel time, then reports
   descriptive statistics, confidence intervals, a paired t-test and a
   Wilcoxon signed-rank test.
2. **Impacted-vehicle analysis** (`impacted_analysis.py`) - computes
   vehicle-level impact metrics for the rerouted or closure-affected
   population when route and tripinfo files are available.
3. **Multi-criteria decision analysis** (`decision.py`) - applies a feasibility
   gate, Pareto screening, additive scoring and weight-robustness sampling.
4. **Report generation** (`report.py`) - formats the evaluation payload into a
   human-readable Markdown report.

Output files are written next to the Monte Carlo output by default:

- `evaluation.json` - full machine-readable evaluation payload.
- `evaluation.md` - human-readable Markdown report.

The evaluator keeps only Monte Carlo runs that contain the full comparable plan
set, so plans are not compared on different run samples.

## Layout

```text
evaluator/
├── __init__.py
├── evaluate.py           # CLI entry point and orchestration
├── metrics.py            # descriptive stats, t-test, Wilcoxon
├── impacted_analysis.py  # vehicle-level rerouting impact metrics
├── decision.py           # feasibility, Pareto, scoring, robustness
└── report.py             # Markdown report generation
```

## Usage

```bash
python -m evaluator.evaluate --input mc_output/
```

Custom output paths can be specified with `--json` and `--markdown`:

```bash
python -m evaluator.evaluate \
    --input mc_output/ \
    --json results/evaluation.json \
    --markdown results/evaluation.md
```

Expected input directory:

```text
mc_output/
├── summary.json
├── run_1/
│   ├── results.json
│   ├── routes.xml
│   ├── routes_<plan_id>.xml
│   ├── baseline/tripinfo.xml
│   └── <plan_id>/tripinfo.xml
└── run_2/
    └── ...
```

`results.json` is required. `summary.json` is strongly recommended because it
records plan ids and closure metadata. When `tripinfo.xml` files are present,
the evaluator refreshes metrics with the current `calc_metrics` implementation.
Route files are only needed when impacted-vehicle identification should be
based on route differences or closed-edge membership instead of the fallback
common-vehicle set.

The main JSON output has this top-level structure:

```json
{
  "metadata": {},
  "paired_delay_vh": {},
  "paired_travel_time_min": {},
  "feasibility": {},
  "impacted_vehicle_analysis": {},
  "decision": {
    "best_plan": "plan_1",
    "ranking": [],
    "pareto_front": [],
    "weight_robustness": {}
  }
}
```

## Dependencies

- Python 3.11+
- `scipy`
- `monte_carlo_simulation` as a sibling package
