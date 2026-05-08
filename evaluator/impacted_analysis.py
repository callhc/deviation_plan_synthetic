"""
impacted_analysis.py
--------------------
Per-vehicle impacted-user metrics for the Monte Carlo evaluator.

Responsibilities
----------------
- Load per-vehicle durations and route lengths from SUMO tripinfo.xml files.
- Compare baseline vs plan at the vehicle level for each run.
- Aggregate run-level user-impact metrics across Monte Carlo runs.
"""

from __future__ import annotations

import statistics
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from scipy import stats

from monte_carlo_simulation.sumo_runner import _is_completed_trip


def get_impacted_vehicle_ids(
    baseline_routes: Path,
    plan_routes: Path,
) -> Set[str]:
    """Return the set of vehicle IDs whose route sequence changed after rerouting.

    Compares <vehicle><route edges="..."/> attributes between the baseline
    and plan route files.  Only vehicles present in both files whose edge
    sequence differs are considered impacted (i.e. actually rerouted).

    Parameters
    ----------
    baseline_routes:
        Pre-detour routes.xml (scenario generator output).
    plan_routes:
        Post-detour routes.xml produced by :func:`precompute_detoured_routes`.
    """
    def _seqs(path: Path) -> Dict[str, str]:
        root = ET.parse(str(path)).getroot()
        result: Dict[str, str] = {}
        for v in root.findall("vehicle"):
            vid = v.get("id", "")
            route_elem = v.find("route")
            # Use explicit `is not None` — ET Elements with no children are
            # falsy, so `elem or fallback` would incorrectly discard them.
            result[vid] = route_elem.get("edges", "") if route_elem is not None else ""
        return result

    baseline_seqs = _seqs(baseline_routes)
    plan_seqs = _seqs(plan_routes)
    return {
        vid for vid, edges in baseline_seqs.items()
        if vid in plan_seqs and edges != plan_seqs[vid]
    }


def _vehicle_ids_crossing_edge(baseline_routes: Path, closed_edge: str) -> Set[str]:
    """Return IDs of vehicles whose baseline route contained *closed_edge*."""
    root = ET.parse(str(baseline_routes)).getroot()
    result: Set[str] = set()
    for vehicle in root.findall("vehicle"):
        route_elem = vehicle.find("route")
        if route_elem is not None and closed_edge in route_elem.get("edges", "").split():
            vid = vehicle.get("id")
            if vid:
                result.add(vid)
    return result


def load_tripinfo_details(tripinfo_path: Path) -> Dict[str, Dict[str, float]]:
    """Return per-vehicle duration and route-length details from a tripinfo XML."""
    if not tripinfo_path.exists():
        raise FileNotFoundError(f"Missing tripinfo file: {tripinfo_path}")

    details: Dict[str, Dict[str, float]] = {}
    root = ET.parse(str(tripinfo_path)).getroot()
    for trip in root.findall("tripinfo"):
        if not _is_completed_trip(trip):
            continue
        vehicle_id = trip.get("id")
        if not vehicle_id:
            continue

        try:
            duration_s = float(trip.get("duration", 0.0))
        except (TypeError, ValueError):
            duration_s = 0.0
        try:
            route_length_m = float(trip.get("routeLength", 0.0))
        except (TypeError, ValueError):
            route_length_m = 0.0

        details[vehicle_id] = {
            "duration_s": duration_s,
            "duration_min": duration_s / 60.0,
            "route_length_m": route_length_m,
        }
    return details


def impacted_metrics_for_run(
    baseline_tripinfo: Path,
    plan_tripinfo: Path,
    impacted_ids: Optional[Set[str]] = None,
) -> Dict[str, float]:
    """Compute vehicle-level impact metrics for one Monte Carlo run.

    Parameters
    ----------
    baseline_tripinfo:
        tripinfo.xml from the baseline (no-detour) SUMO run.
    plan_tripinfo:
        tripinfo.xml from the plan (detoured) SUMO run.
    impacted_ids:
        When provided, restrict all metrics to these vehicle IDs (the rerouted
        or closure-affected population).  Pass an empty set to get n=0 rather
        than silently diluting with the full vehicle fleet.  When None,
        all vehicles common to both tripinfo files are used (legacy behaviour,
        produces network-wide rather than user-impact metrics).
    """
    baseline_details = load_tripinfo_details(baseline_tripinfo)
    plan_details = load_tripinfo_details(plan_tripinfo)

    if impacted_ids is not None:
        vehicle_ids = [
            vid for vid in impacted_ids
            if vid in baseline_details and vid in plan_details
        ]
    else:
        vehicle_ids = [vid for vid in plan_details if vid in baseline_details]

    if not vehicle_ids:
        return {
            "delta_t_min": 0.0,
            "phi_net": 0.0,
            "v_net_kmh": 0.0,
            "vehicles_compared": 0,
        }

    delta_t_min_total = 0.0
    delayed_count = 0
    route_len_km_sum = 0.0
    duration_h_sum = 0.0

    for vehicle_id in vehicle_ids:
        baseline = baseline_details[vehicle_id]
        plan = plan_details[vehicle_id]
        delta_t_min_total += plan["duration_min"] - baseline["duration_min"]
        if plan["duration_min"] > baseline["duration_min"] + 1e-3:
            delayed_count += 1
        route_len_km_sum += plan["route_length_m"] / 1000.0
        duration_h_sum += plan["duration_min"] / 60.0

    vehicle_count = len(vehicle_ids)
    phi_net = delayed_count / vehicle_count if vehicle_count > 0 else 0.0
    v_net_kmh = route_len_km_sum / duration_h_sum if duration_h_sum > 0 else 0.0

    return {
        "delta_t_min": delta_t_min_total,
        "phi_net": phi_net,
        "v_net_kmh": v_net_kmh,
        "vehicles_compared": vehicle_count,
    }


def summarize_impacted_metric(values: Iterable[float]) -> Dict[str, Any]:
    """Return summary statistics and a 95% t-interval for one impacted metric."""
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
    ci_low = mean
    ci_high = mean
    if len(seq) > 1:
        sem = stats.sem(seq)
        t_crit = stats.t.ppf(0.975, len(seq) - 1)
        margin = float(t_crit * sem)
        ci_low = mean - margin
        ci_high = mean + margin

    return {
        "n": len(seq),
        "mean": mean,
        "std": statistics.stdev(seq) if len(seq) > 1 else 0.0,
        "min": min(seq),
        "max": max(seq),
        "median": statistics.median(seq),
        "ci95_low": ci_low,
        "ci95_high": ci_high,
    }


def collect_impacted_analysis(
    input_dir: Path,
    plan_ids: List[str],
    run_dirs: List[Path],
    *,
    baseline_routes: Optional[Path] = None,
    closed_edge: Optional[str] = None,
) -> Dict[str, Any]:
    """Collect impacted-vehicle metrics across all runs for each plan.

    Impacted vehicles are identified in priority order:

    1. Route comparison (preferred): vehicles whose pre-computed plan
       route file differs from the baseline routes file.  The plan route
       files are expected at input_dir/routes_<plan_id>.xml.
    2. Closed-edge filter: vehicles whose baseline route contained
       closed_edge (used when plan route files are absent but
       baseline_routes and closed_edge are both provided).
    3. All common vehicles (legacy fallback): used when neither route
       files nor closed_edge information is available.  Produces
       network-wide metrics rather than true user-impact metrics; a
       diagnostic is printed to stdout.

    Parameters
    ----------
    input_dir:
        Root MC output directory (contains routes_<plan_id>.xml files).
    plan_ids:
        Deviation plan identifiers to analyse (excluding baseline).
    run_dirs:
        Per-run subdirectories sorted by run index.
    baseline_routes:
        Original demand routes.xml from the scenario generator, used to
        identify which vehicles were affected by the closure.
    closed_edge:
        The closed edge ID; used as a fallback when route files are absent.
    """
    per_plan: Dict[str, Dict[str, Any]] = {}

    for plan_id in plan_ids:
        plan_routes_path = input_dir / f"routes_{plan_id}.xml"
        impacted_ids: Optional[Set[str]] = None
        impacted_source: str

        if (
            plan_routes_path.exists()
            and baseline_routes is not None
            and baseline_routes.exists()
        ):
            impacted_ids = get_impacted_vehicle_ids(baseline_routes, plan_routes_path)
            impacted_source = "route-diff (baseline vs pre-computed plan routes)"
        elif (
            baseline_routes is not None
            and baseline_routes.exists()
            and closed_edge is not None
        ):
            impacted_ids = _vehicle_ids_crossing_edge(baseline_routes, closed_edge)
            impacted_source = f"closed-edge filter ('{closed_edge}' in baseline route)"
        else:
            impacted_source = "all common vehicles (no route files available — network-wide metric)"
            print(
                f"  [impacted_analysis] WARNING: No route files or closed_edge provided for "
                f"'{plan_id}'. Falling back to all common vehicles; metrics will be "
                f"network-wide, not user-impact specific."
            )

        if impacted_ids is not None and not impacted_ids:
            print(
                f"  [impacted_analysis] WARNING: No impacted vehicles identified for "
                f"'{plan_id}' via {impacted_source}. Returning n=0."
            )

        rows = []
        for run_dir in run_dirs:
            run_str = run_dir.name.split("_", 1)[1]
            try:
                run_id = int(run_str)
            except ValueError:
                continue

            metrics = impacted_metrics_for_run(
                run_dir / "baseline" / "tripinfo.xml",
                run_dir / plan_id / "tripinfo.xml",
                impacted_ids=impacted_ids,
            )
            rows.append({"run": run_id, **metrics})

        per_plan[plan_id] = {
            "impacted_vehicles_source": impacted_source,
            "impacted_vehicles_count": len(impacted_ids) if impacted_ids is not None else None,
            "per_run": rows,
            "metrics": {
                "delta_t_min": summarize_impacted_metric(row["delta_t_min"] for row in rows),
                "phi_net": summarize_impacted_metric(row["phi_net"] for row in rows),
                "v_net_kmh": summarize_impacted_metric(row["v_net_kmh"] for row in rows),
                "vehicles_compared": summarize_impacted_metric(
                    row["vehicles_compared"] for row in rows
                ),
            },
        }

    return {"per_plan": per_plan}
