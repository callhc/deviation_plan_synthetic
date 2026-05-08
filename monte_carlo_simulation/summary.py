"""Summary statistics for Monte Carlo results.

Aggregates per-run results into descriptive statistics across all iterations
and formats them for the final summary.json output file.
"""

from __future__ import annotations

import statistics
from typing import Any


def _stats(values: list[float]) -> dict[str, float]:
    """Compute descriptive statistics for a list of floats.

    Parameters
    ----------
    values:
        Sample values to summarise.

    Returns
    -------
    dict[str, float]
        Keys: mean, std, min, max, median.
        All values are 0.0 when values is empty.
    """
    if not values:
        return {"mean": 0.0, "std": 0.0, "min": 0.0, "max": 0.0, "median": 0.0}
    return {
        "mean": statistics.mean(values),
        "std": statistics.stdev(values) if len(values) > 1 else 0.0,
        "min": min(values),
        "max": max(values),
        "median": statistics.median(values),
    }


def generate_summary(
    all_results: list[dict[str, Any]],
    closed_edge: str,
    plans: list[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate per-run results into a summary with descriptive statistics.

    Parameters
    ----------
    all_results:
        One dict per completed Monte Carlo run, as returned by
        run_iteration in monte_carlo_simulation.simulation.
    closed_edge:
        The edge id whose closure was simulated.
    plans:
        List of plan dicts loaded from the JSON plan file (used to
        enumerate plan ids).

    Returns
    -------
    dict[str, Any]
        Top-level keys:

        metadata
            Run count, closed edge, and plan ids.
        plans
            Per-plan statistics for time-loss delay_vh, travel_time_min,
            completed_trips, and optionally rerouted_vehicles
            and rerouted_pct.
    """
    plan_ids = ["baseline"] + [p["plan_id"] for p in plans]
    summary: dict[str, Any] = {
        "metadata": {
            "closed_edge": closed_edge,
            "total_runs": len(all_results),
            "plans": plan_ids,
        },
        "plans": {},
    }

    for plan_id in plan_ids:
        rows = [r[plan_id] for r in all_results if plan_id in r]

        plan_summary: dict[str, Any] = {
            "delay_vh": _stats([r["delay"] for r in rows]),
            "travel_time_min": _stats([r["travel_time"] for r in rows]),
            "completed_trips": _stats([r["completed"] for r in rows]),
        }

        if any("rerouted" in r for r in rows):
            plan_summary["rerouted_vehicles"] = _stats(
                [r["rerouted"] for r in rows if "rerouted" in r]
            )
            plan_summary["rerouted_pct"] = _stats(
                [r["rerouted_pct"] for r in rows if "rerouted_pct" in r]
            )

        summary["plans"][plan_id] = plan_summary

    return summary
