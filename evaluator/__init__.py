"""Evaluator — statistical analysis and multi-criteria decision for Monte Carlo outputs.

The package is organised around four modules:

1. evaluator.metrics — descriptive statistics, paired t-test and Wilcoxon
   signed-rank test on per-run metric deltas.
2. evaluator.impacted_analysis — per-vehicle impact metrics (delta_t, phi_net,
   v_net) restricted to the rerouted population.
3. evaluator.decision — MCDA pipeline: feasibility gate, Pareto screening,
   additive weighted scoring, and Dirichlet weight-robustness analysis.
4. evaluator.report — Markdown report generation from the evaluation dict.

The public entry point is evaluator.evaluate, which reads a Monte Carlo output
directory and writes evaluation.json and evaluation.md.
"""

from evaluator.metrics import paired_metric_analysis, summarize, paired_tests
from evaluator.decision import choose_best_plan_multicriteria, collect_feasibility
from evaluator.report import build_markdown_report

__all__ = [
    "paired_metric_analysis",
    "summarize",
    "paired_tests",
    "choose_best_plan_multicriteria",
    "collect_feasibility",
    "build_markdown_report",
]
