"""
metrics.py
----------
Statistical primitives used by the Monte Carlo evaluator.

Responsibilities
----------------
- Descriptive summaries (mean, std, CI) of a sample of floats.
- Paired statistical tests against a null of zero effect.
- Per-metric paired analysis across simulation runs.
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Dict, Iterable, List

from scipy import stats


# ---------------------------------------------------------------------------
# Descriptive summary
# ---------------------------------------------------------------------------

def summarize(values: Iterable[float]) -> Dict[str, Any]:
    """Return descriptive statistics and a 95 % Student-t confidence interval.

    Parameters
    ----------
    values:
        An iterable of floats (e.g. per-run paired deltas).

    Returns
    -------
    dict with keys: n, mean, std, min, max, median, ci95_low, ci95_high.
    All float fields are 0.0 when the sample is empty.
    """
    seq = list(values)
    if not seq:
        return {
            "n": 0,
            "mean": 0.0,
            "std": 0.0,
            "min": 0.0,
            "max": 0.0,
            "median": 0.0,
            "ci95_low": 0.0,
            "ci95_high": 0.0,
        }

    mean = statistics.mean(seq)
    std = statistics.stdev(seq) if len(seq) > 1 else 0.0
    ci_low = mean
    ci_high = mean

    if len(seq) > 1:
        sem = stats.sem(seq)
        t_crit = stats.t.ppf(0.975, df=len(seq) - 1)
        margin = float(t_crit * sem)
        ci_low = mean - margin
        ci_high = mean + margin

    return {
        "n": len(seq),
        "mean": mean,
        "std": std,
        "min": min(seq),
        "max": max(seq),
        "median": statistics.median(seq),
        "ci95_low": ci_low,
        "ci95_high": ci_high,
    }


# ---------------------------------------------------------------------------
# Hypothesis tests
# ---------------------------------------------------------------------------

def paired_tests(values: Iterable[float]) -> Dict[str, Any]:
    """Run paired one-sample t-test and Wilcoxon signed-rank test against μ = 0.

    Both tests ask: "Is the mean paired difference significantly different from
    zero?"  Using two complementary tests (parametric + non-parametric) increases
    robustness when the normality assumption is uncertain, which is common for
    traffic simulation outputs.

    Parameters
    ----------
    values:
        Sequence of paired differences (plan_metric − baseline_metric), one per
        Monte Carlo run.

    Returns
    -------
    dict with sub-dicts ttest and wilcoxon, each containing statistic and
    pvalue, or a note when the test cannot be run.
    """
    seq = list(values)

    _not_enough = {
        "statistic": None,
        "pvalue": None,
        "note": "At least 2 runs required.",
    }
    if len(seq) < 2:
        return {"ttest": _not_enough, "wilcoxon": _not_enough}

    # All-zero differences → tests are undefined / trivially non-significant.
    if all(math.isclose(v, 0.0, abs_tol=1e-12) for v in seq):
        _all_zero = {
            "statistic": None,
            "pvalue": None,
            "note": "All paired differences are zero.",
        }
        return {"ttest": _all_zero, "wilcoxon": _all_zero}

    # Parametric test (assumes approximate normality of differences).
    ttest_res = stats.ttest_1samp(seq, popmean=0.0)
    ttest: Dict[str, Any] = {
        "statistic": float(ttest_res.statistic),
        "pvalue": float(ttest_res.pvalue),
    }

    # Non-parametric test (distribution-free, more robust for small n).
    try:
        wilcoxon_res = stats.wilcoxon(
            seq,
            zero_method="wilcox",
            alternative="two-sided",
            mode="auto",
        )
        wilcoxon: Dict[str, Any] = {
            "statistic": float(wilcoxon_res.statistic),
            "pvalue": float(wilcoxon_res.pvalue),
        }
    except ValueError as exc:
        wilcoxon = {"statistic": None, "pvalue": None, "note": str(exc)}

    return {"ttest": ttest, "wilcoxon": wilcoxon}


# ---------------------------------------------------------------------------
# Per-plan paired analysis
# ---------------------------------------------------------------------------

def paired_metric_analysis(
    run_results: List[Dict[str, Any]],
    plan_id: str,
    metric_key: str,
) -> Dict[str, Any]:
    """Compute per-run paired deltas and their summary statistics for one metric.

    For each Monte Carlo run the delta is defined as:

        delta = plan[metric_key] − baseline[metric_key]

    A positive delta therefore means the plan worsens the metric relative to
    the unmodified network.

    Parameters
    ----------
    run_results:
        List of per-run result dicts, each containing at least a baseline
        key and a key for plan_id.
    plan_id:
        Identifier of the deviation plan to analyse.
    metric_key:
        The metric to extract from each result dict (for example delay, which
        stores SUMO time-loss delay in vehicle-hours).

    Returns
    -------
    dict with keys per_run, summary and tests_against_zero.
    """
    per_run: List[Dict[str, Any]] = []
    deltas: List[float] = []

    for result in run_results:
        baseline = result.get("baseline")
        plan = result.get(plan_id)
        if not baseline or not plan:
            continue
        delta = float(plan[metric_key]) - float(baseline[metric_key])
        per_run.append(
            {
                "run": int(plan["run"]),
                "baseline": float(baseline[metric_key]),
                "plan": float(plan[metric_key]),
                "delta": delta,
            }
        )
        deltas.append(delta)

    return {
        "per_run": per_run,
        "summary": summarize(deltas),
        "tests_against_zero": paired_tests(deltas),
    }
