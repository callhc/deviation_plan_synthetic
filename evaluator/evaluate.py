"""
evaluate.py
-----------
CLI entry point for the Monte Carlo detour-plan evaluator.

Usage
-----
    python -m evaluator.evaluate --input path/to/mc_output

This module is intentionally thin: it handles argument parsing, I/O, and
orchestration only.  All statistical logic lives in metrics.py, all
decision logic in decision.py, and all report formatting in report.py.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from evaluator.decision import (
    choose_best_plan_multicriteria,
    collect_feasibility,
)
from evaluator.impacted_analysis import collect_impacted_analysis
from evaluator.metrics import paired_metric_analysis
from evaluator.report import build_markdown_report
from monte_carlo_simulation.sumo_runner import calc_metrics


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_json(path: Path) -> Dict[str, Any]:
    """Read and parse a JSON file."""
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    """Serialise payload to a pretty-printed JSON file."""
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Run-directory discovery
# ---------------------------------------------------------------------------

def list_run_dirs(input_dir: Path) -> List[Path]:
    """Return subdirectories named run_<N> sorted by N.

    Directories whose suffix cannot be parsed as an integer are placed last.
    """
    def _run_key(path: Path) -> int:
        try:
            return int(path.name.split("_", 1)[1])
        except (IndexError, ValueError):
            return math.inf  # type: ignore[return-value]

    return sorted(
        [p for p in input_dir.iterdir() if p.is_dir() and p.name.startswith("run_")],
        key=_run_key,
    )


def extract_run_id(run_dir: Path) -> int:
    """Parse run_<N> and return N."""
    return int(run_dir.name.split("_", 1)[1])


# ---------------------------------------------------------------------------
# Result collection
# ---------------------------------------------------------------------------

def refresh_metrics_from_tripinfo(
    run_dir: Path,
    results: Dict[str, Any],
) -> Dict[str, Any]:
    """Refresh available SUMO metrics from tripinfo.xml files.

    Older Monte Carlo outputs may have stored delay from SUMO waitingTime in
    results.json.  When the corresponding tripinfo.xml file is still present,
    recomputing through calc_metrics makes the evaluator use the current
    SUMO timeLoss definition while preserving plan/run/rerouting metadata.
    """
    refreshed: Dict[str, Any] = {}
    for plan_id, payload in results.items():
        row = dict(payload)
        tripinfo_path = run_dir / plan_id / "tripinfo.xml"
        if tripinfo_path.exists():
            metrics = calc_metrics(tripinfo_path)
            row.update(metrics)
        refreshed[plan_id] = row
    return refreshed

def collect_run_results(
    input_dir: Path,
) -> Tuple[List[Dict[str, Any]], List[str], Dict[str, Any]]:
    """Load per-run payloads and infer plan identifiers.

    Parameters
    ----------
    input_dir:
        Root MC output directory containing summary.json and run_*/
        subdirectories.

    Returns
    -------
    (run_items, plan_ids, summary)
        run_items is a list of dicts with run_id, run_dir, and results keys.
        plan_ids includes "baseline" and all deviation plan keys.
        summary is the top-level summary.json dict (empty dict if absent).
    """
    summary_path = input_dir / "summary.json"
    summary = load_json(summary_path) if summary_path.exists() else {}
    plans: Optional[List[str]] = summary.get("metadata", {}).get("plans")

    run_items: List[Dict[str, Any]] = []
    for run_dir in list_run_dirs(input_dir):
        results_path = run_dir / "results.json"
        if results_path.exists():
            raw_results = load_json(results_path)
            run_items.append(
                {
                    "run_id": extract_run_id(run_dir),
                    "run_dir": run_dir,
                    "results": refresh_metrics_from_tripinfo(run_dir, raw_results),
                }
            )

    # Fall back to inferring plan keys from the first result dict.
    if not plans and run_items:
        first = run_items[0]["results"]
        plans = ["baseline"] + [k for k in first.keys() if k != "baseline"]

    return run_items, plans or [], summary


def filter_complete_runs(
    run_items: List[Dict[str, Any]],
    required_plan_ids: List[str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Retain only runs that contain the full comparable plan set.

    This prevents silent bias where some plans would otherwise be analysed on
    fewer runs than others due to missing per-run outputs.
    """
    required_keys = set(required_plan_ids)
    comparable_runs: List[Dict[str, Any]] = []
    excluded_runs: List[Dict[str, Any]] = []

    for item in run_items:
        missing_keys = sorted(required_keys.difference(item["results"].keys()))
        if missing_keys:
            excluded_runs.append(
                {
                    "run": item["run_id"],
                    "missing_result_keys": missing_keys,
                }
            )
            continue
        comparable_runs.append(item)

    return comparable_runs, excluded_runs


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate Monte Carlo outputs with paired statistics and MCDA."
    )
    parser.add_argument(
        "--input", "-i",
        type=Path,
        required=True,
        help="Root output directory from the MC simulation.",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="Destination path for the evaluation JSON (default: <input>/evaluation.json).",
    )
    parser.add_argument(
        "--markdown",
        type=Path,
        default=None,
        help="Destination path for the Markdown report (default: <input>/evaluation.md).",
    )
    return parser


def main() -> None:
    """Orchestrate the full evaluation pipeline."""
    args = _build_arg_parser().parse_args()

    input_dir = args.input.resolve()
    run_items, all_plan_ids, summary = collect_run_results(input_dir)

    if not run_items:
        raise SystemExit(f"No run results found under {input_dir}")
    if not all_plan_ids:
        raise SystemExit(f"Could not infer plan ids from {input_dir}")

    total_runs_available = len(run_items)
    run_items, excluded_incomplete_runs = filter_complete_runs(run_items, all_plan_ids)
    if not run_items:
        raise SystemExit(
            "No runs contain the full comparable plan set. "
            "Check the per-run outputs for missing plan results."
        )

    run_results = [item["results"] for item in run_items]
    run_dirs = [item["run_dir"] for item in run_items]

    non_baseline_ids = [pid for pid in all_plan_ids if pid != "baseline"]
    if not non_baseline_ids:
        raise SystemExit("No deviation plans found in the output.")

    # --- Statistical analysis per metric ----------------------------------
    paired_delay_vh = {
        pid: paired_metric_analysis(run_results, pid, "delay")
        for pid in non_baseline_ids
    }
    paired_travel_time_min = {
        pid: paired_metric_analysis(run_results, pid, "travel_time")
        for pid in non_baseline_ids
    }

    # --- Impacted-vehicle and feasibility analyses ------------------------
    meta = summary.get("metadata", {})
    _baseline_routes_str = meta.get("input_demand_file")
    _baseline_routes = Path(_baseline_routes_str) if _baseline_routes_str else None
    impacted_vehicle_analysis = collect_impacted_analysis(
        input_dir,
        non_baseline_ids,
        run_dirs,
        baseline_routes=_baseline_routes,
        closed_edge=meta.get("closed_edge"),
    )
    feasibility = collect_feasibility(run_results, non_baseline_ids)

    # --- Multi-criteria decision ------------------------------------------
    decision = choose_best_plan_multicriteria(
        paired_delay_vh,
        paired_travel_time_min,
        impacted_vehicle_analysis,
        feasibility,
        non_baseline_ids,
    )

    # --- Assemble and write outputs ---------------------------------------
    evaluation: Dict[str, Any] = {
        "metadata": {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "input_dir": str(input_dir),
            "closed_edge": summary.get("metadata", {}).get("closed_edge"),
            "delay_metric": "sum_sumo_timeLoss_completed_trips_vh",
            "total_runs": len(run_results),
            "total_runs_available": total_runs_available,
            "excluded_incomplete_runs": excluded_incomplete_runs,
            "run_ids_analyzed": [item["run_id"] for item in run_items],
            "plans": all_plan_ids,
        },
        "paired_delay_vh": paired_delay_vh,
        "paired_travel_time_min": paired_travel_time_min,
        "feasibility": feasibility,
        "impacted_vehicle_analysis": impacted_vehicle_analysis,
        "decision": decision,
    }

    json_path = args.json.resolve() if args.json else input_dir / "evaluation.json"
    markdown_path = args.markdown.resolve() if args.markdown else input_dir / "evaluation.md"

    write_json(json_path, evaluation)
    markdown_path.write_text(build_markdown_report(evaluation), encoding="utf-8")

    print(f"Wrote evaluation JSON:     {json_path}")
    print(f"Wrote evaluation Markdown: {markdown_path}")
    print(f"Best plan:                 {decision['best_plan']}")


if __name__ == "__main__":
    main()
