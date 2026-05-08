#!/usr/bin/env python3
"""
Monte Carlo simulation for comparing deviation plans.

Each iteration independently routes the baseline demand through duarouter
with randomised edge weights via --weights.random-factor, producing a
different set of routes per run.  Detour plans are then applied to each
run's routes file before SUMO is executed for every scenario (baseline +
one per plan).

Stochasticity model
-------------------
Variation across iterations comes from the routing step: duarouter draws
a per-edge cost multiplier from Uniform[1.0, random_factor] at each run,
so different drivers (iterations) perceive different travel costs and
therefore choose different paths.  This models day-to-day variability in
route choice and produces genuinely different baseline route files each run —
unlike a fixed-routes design where the only variance is SUMO's internal
behavioural noise.

Usage
-----
    python -m monte_carlo_simulation.simulation \\
        --net   network.net.xml \\
        --plan  detours.json \\
        --trips trips.xml \\
        --runs  30

The trips.xml is produced by scenario_generator:

    python -m scenario_generator.generator config.toml

JSON plan format (detours.json) — as produced by deviation_plan_maker:

    {
        "closed_edge": "edge_id",
        "plans": [
            {
                "plan_id": "plan_1",
                "detour_edges": ["e1", "e2", "e3"],
                "source_node": "node_a",
                "target_node": "node_b"
            }
        ]
    }

source_node and target_node are optional; when absent the detour
splice starts at the closed edge's immediate upstream junction (depth-0
fallback derived from the network file).
"""

import argparse
import json
import math
import os
import xml.etree.ElementTree as ET
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from deviation_plan_maker.deviation_plan import DeviationPlan, PathResult
from deviation_plan_maker.route_rewriter import apply_detour_to_routes

from monte_carlo_simulation.trip_utils import (
    count_rerouted_vehicles,
    get_trip_time_window,
    load_edge_endpoints,
)
from monte_carlo_simulation.sumo_runner import calc_metrics, run_duarouter, run_sumo
from monte_carlo_simulation.summary import generate_summary


# =============================================================================
# HELPERS
# =============================================================================


def _plan_dict_to_deviation_plan(
    plan: Dict[str, Any],
    fallback_source_node: Optional[str],
) -> DeviationPlan:
    """Convert a plan dict (from the JSON file) to a DeviationPlan.

    depth, rank, and total_length_m are preserved from the JSON
    when present.  For legacy JSON without these fields, depth=0 is
    inferred — safe for Yen/manual plans but NOT for upstream plans, which
    must carry explicit depth values so that the rerouting policy is built
    correctly.

    source_node is read from the dict when present; otherwise
    fallback_source_node (the closed edge's immediate upstream junction)
    is used, giving a depth-0 universal fallback.
    """
    source_node = plan.get("source_node") or fallback_source_node
    return DeviationPlan(
        plan_id=plan["plan_id"],
        rank=plan.get("rank", 1),
        depth=plan.get("depth", 0),
        path=PathResult(
            nodes=[],
            edges=plan["detour_edges"],
            cumulative_costs=[],
        ),
        total_length_m=plan.get("total_length_m", 0.0),
        source_node=source_node,
        target_node=plan.get("target_node"),
    )


def _validate_no_closed_edge(route_file: Path, closed_edge: str) -> None:
    """Raise ValueError if any route still contains the closed edge.

    Parses the written route file and checks every vehicle/trip edge sequence
    for an exact occurrence of *closed_edge* (i.e. as a whitespace-delimited
    token, not as a substring of another edge id).  Fails loudly so the
    pipeline never silently produces an invalid simulation scenario.

    Parameters
    ----------
    route_file:
        Path to a SUMO routes.xml file to validate.
    closed_edge:
        Edge id that must not appear in any route sequence.

    Raises
    ------
    ValueError
        If one or more vehicles still traverse the closed edge.
    """
    root = ET.parse(str(route_file)).getroot()
    violating: List[str] = []
    for vehicle in root.findall("vehicle"):
        route_elem = vehicle.find("route")
        if route_elem is not None and closed_edge in route_elem.get("edges", "").split():
            violating.append(vehicle.get("id", "?"))
    for trip in root.findall("trip"):
        if closed_edge in trip.get("edges", "").split():
            violating.append(trip.get("id", "?"))
    if violating:
        raise ValueError(
            f"Route rewriting incomplete: {len(violating)} vehicle(s) in "
            f"'{route_file.name}' still use closed edge '{closed_edge}'. "
            f"First offenders: {violating[:5]}. "
            f"Ensure a depth-0 fallback plan exists for every affected vehicle."
        )


# =============================================================================
# MONTE CARLO ITERATION
# =============================================================================


def run_iteration(args: tuple) -> Dict:
    """Run one Monte Carlo iteration: route → detour rewrite → SUMO for all plans.

    Each iteration independently routes the baseline demand through duarouter
    with a unique seed, producing a different set of routes.  Detour plans
    are then applied to those routes before simulation, so both routing
    variation and detour effectiveness are captured in each run.

    .. note::
        Arguments are packed into a single tuple so this function can be
        passed directly to concurrent.futures.ProcessPoolExecutor.submit,
        which requires a picklable callable with a single positional argument.

    Parameters
    ----------
    args:
        Positional tuple (net_xml, trips_file, plans, edge_endpoints,
        closed_edge, output_dir, run_id, begin, end, random_factor) where:

        - net_xml:        Path to the SUMO network file.
        - trips_file:     Path to the trips.xml from scenario_generator.
        - plans:          List of plan dicts from the JSON plan file.
        - edge_endpoints: Mapping edge_id to (from_node, to_node).
        - closed_edge:    The edge id whose closure is being evaluated.
        - output_dir:     Root output directory; a run_<id> sub-directory is created.
        - run_id:         Integer iteration index, also used as the duarouter seed.
        - begin:          Simulation start time in seconds.
        - end:            Simulation end time in seconds.
        - random_factor:  Upper bound for duarouter's per-edge cost multiplier (≥ 1.0).

    Returns
    -------
    dict
        Per-plan metric dicts keyed by plan id, including "baseline".
    """
    (
        net_xml, trips_file, plans, edge_endpoints,
        closed_edge, output_dir, run_id, begin, end, random_factor,
    ) = args

    run_dir = output_dir / f"run_{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # ---- Step 1: route baseline trips with perturbed edge costs ----------
    baseline_routes = run_dir / "routes.xml"
    run_duarouter(trips_file, net_xml, baseline_routes, seed=run_id, random_factor=random_factor)

    # ---- Step 2: apply detour plans to this run's baseline routes --------
    fallback_source = edge_endpoints.get(closed_edge, (None, None))[0]
    all_dev_plans = [_plan_dict_to_deviation_plan(p, fallback_source) for p in plans]
    is_upstream_set = any(p.depth > 0 for p in all_dev_plans)
    depth0_plans = [p for p in all_dev_plans if p.depth == 0]

    if is_upstream_set and not depth0_plans:
        raise ValueError(
            f"Upstream plan set for closed edge '{closed_edge}' has no depth-0 fallback "
            f"plan.  Every upstream plan must be paired with a depth-0 plan that "
            f"covers vehicles immediately upstream of the closure.  "
            f"Re-generate plans with max_depth >= 0 or add a manual depth-0 entry."
        )

    plan_route_files: Dict[str, Path] = {}
    rerouted_stats: Dict[str, Dict[str, Any]] = {}

    for dev_plan in all_dev_plans:
        plan_id = dev_plan.plan_id
        plan_routes = run_dir / f"routes_{plan_id}.xml"

        # Build the routing policy (depth-aware): upstream plans need to be
        # paired with the depth-0 fallback so every affected vehicle is covered.
        if is_upstream_set and dev_plan.depth > 0:
            policy: List[DeviationPlan] = sorted(
                [dev_plan] + depth0_plans,
                key=lambda p: (-p.depth, p.total_length_m),
            )
        else:
            policy = [dev_plan]

        apply_detour_to_routes(
            baseline_routes, plan_routes, policy, [closed_edge], edge_endpoints
        )
        _validate_no_closed_edge(plan_routes, closed_edge)

        rerouted, total = count_rerouted_vehicles(baseline_routes, plan_routes)
        rerouted_pct = round(rerouted / total * 100, 2) if total > 0 else 0.0
        rerouted_stats[plan_id] = {
            "rerouted": rerouted,
            "rerouted_pct": rerouted_pct,
            "total_vehicles": total,
        }
        plan_route_files[plan_id] = plan_routes

    # ---- Step 3: simulate baseline + each plan ---------------------------
    results: Dict[str, Any] = {}

    baseline_metrics = calc_metrics(
        run_sumo(net_xml, baseline_routes, run_dir / "baseline", run_id, begin, end)
    )
    baseline_metrics.update({"plan": "baseline", "run": run_id})
    results["baseline"] = baseline_metrics

    for dev_plan in all_dev_plans:
        plan_id = dev_plan.plan_id
        metrics = calc_metrics(
            run_sumo(net_xml, plan_route_files[plan_id], run_dir / plan_id, run_id, begin, end)
        )
        metrics.update({"plan": plan_id, "run": run_id, **rerouted_stats.get(plan_id, {})})
        results[plan_id] = metrics

    # Persist per-run results
    (run_dir / "results.json").write_text(json.dumps(results, indent=2))
    summary_line = " | ".join(f"{k}={v['delay']:.2f}vh" for k, v in results.items())
    print(f"[Run {run_id:>3}] {summary_line}")
    return results


def list_existing_completed_runs(output_dir: Path) -> List[Tuple[int, Dict]]:
    """Return completed run payloads already present in *output_dir*."""
    existing: List[Tuple[int, Dict]] = []
    for run_dir in output_dir.iterdir():
        if not run_dir.is_dir() or not run_dir.name.startswith("run_"):
            continue
        try:
            run_id = int(run_dir.name.split("_", 1)[1])
        except (IndexError, ValueError):
            continue
        results_path = run_dir / "results.json"
        if not results_path.exists():
            continue
        existing.append((run_id, json.loads(results_path.read_text(encoding="utf-8"))))
    existing.sort(key=lambda item: item[0])
    return existing


# =============================================================================
# CLI
# =============================================================================


def build_parser() -> argparse.ArgumentParser:
    """Build and return the CLI argument parser."""
    p = argparse.ArgumentParser(
        description="Monte Carlo Simulation for Deviation Plans",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--net", "-n", type=Path, required=True, help="Network file (.net.xml)")
    p.add_argument("--plan", type=Path, required=True, help="JSON file with closed_edge and plans")
    p.add_argument(
        "--trips", "-t", type=Path, required=True,
        help=(
            "Trips file (trips.xml) produced by scenario_generator. "
            "Each Monte Carlo iteration re-routes this demand through duarouter "
            "with randomised edge weights."
        ),
    )
    p.add_argument("--runs", "-r", type=int, default=10, help="Number of Monte Carlo runs")
    p.add_argument("--parallel", "-p", type=int, default=1, help="Parallel workers (0 = all CPUs)")
    p.add_argument("--output", "-o", type=Path, default=Path("mc_output"), help="Output directory")
    p.add_argument(
        "--resume",
        action="store_true",
        help="Resume from existing completed runs in the output directory",
    )
    p.add_argument(
        "--random-factor", type=float, default=1.2,
        help=(
            "Upper bound for duarouter's per-edge cost multiplier "
            "(passed as --weights.random-factor).  Each edge weight is scaled "
            "by a value drawn uniformly from [1.0, random-factor] per run, "
            "modelling day-to-day variability in perceived travel costs."
        ),
    )
    p.add_argument("--begin", type=float, help="Simulation start time in seconds")
    p.add_argument("--end", type=float, help="Simulation end time in seconds")
    return p


def main() -> None:
    """CLI entry point for the Monte Carlo simulation."""
    parser = build_parser()
    args = parser.parse_args()

    # --- Load plan ---
    with open(args.plan, encoding="utf-8") as f:
        plan_data = json.load(f)
    closed_edge = plan_data["closed_edge"]
    plans = plan_data["plans"]

    # --- Validate trips file ---
    trips_file = args.trips.resolve()
    if not trips_file.exists():
        parser.error(f"Trips file not found: {trips_file}")

    # --- Print header ---
    SEP = "=" * 60
    print(f"\n{SEP}\nMONTE CARLO SIMULATION\n{SEP}")
    print(f"Network      : {args.net}")
    print(f"Trips file   : {trips_file}")
    print(f"Closed edge  : {closed_edge}")
    print(f"Plans        : {len(plans)}")
    for p in plans:
        print(f"  - {p['plan_id']}: {len(p['detour_edges'])} detour edges")
    print(f"Runs         : {args.runs}")
    print(f"Random factor: {args.random_factor}\n{SEP}\n")

    # --- Simulation window ---
    args.output.mkdir(parents=True, exist_ok=True)
    min_depart, max_depart = get_trip_time_window(trips_file)
    sim_begin = math.floor(args.begin if args.begin is not None else min_depart)
    sim_end = math.ceil(
        args.end if args.end is not None else max(max_depart + 3600.0, sim_begin + 3600.0)
    )
    if sim_end <= sim_begin:
        parser.error(f"Invalid simulation window: begin={sim_begin}, end={sim_end}")
    print(f"Simulation window: {sim_begin} → {sim_end} s\n")

    # --- Edge endpoints (needed for detour rewriting every iteration) ---
    edge_endpoints = load_edge_endpoints(args.net)

    # --- Resume handling ---
    existing_results = list_existing_completed_runs(args.output) if args.resume else []
    existing_run_count = len(existing_results)
    if args.resume and existing_run_count:
        next_run_id = existing_results[-1][0] + 1
        print(
            f"Resuming from {existing_run_count} completed runs "
            f"(next run id: {next_run_id})\n"
        )
    if args.resume and args.runs < existing_run_count:
        parser.error(
            f"Requested total runs ({args.runs}) is smaller than the number of "
            f"completed runs already present ({existing_run_count})."
        )

    first_new_run = existing_results[-1][0] + 1 if existing_results else 1
    run_args = [
        (
            args.net, trips_file, plans, edge_endpoints,
            closed_edge, args.output, run_id, sim_begin, sim_end, args.random_factor,
        )
        for run_id in range(first_new_run, args.runs + 1)
    ]
    workers = args.parallel if args.parallel > 0 else (os.cpu_count() or 4)
    all_results: List[Dict] = [payload for _, payload in existing_results]

    def run_sequential() -> None:
        print(f"Running {len(run_args)} new iterations sequentially...\n")
        for a in run_args:
            all_results.append(run_iteration(a))

    try:
        if not run_args:
            print("No new iterations required; using existing completed runs.\n")
        elif workers > 1:
            print(f"Running {len(run_args)} new iterations with {workers} workers...\n")
            try:
                with ProcessPoolExecutor(max_workers=workers) as executor:
                    futures = {executor.submit(run_iteration, a): a[6] for a in run_args}
                    for future in as_completed(futures):
                        all_results.append(future.result())
            except OSError as exc:
                if exc.errno != 1:
                    raise
                print(
                    f"Parallel execution not available ({exc}). "
                    "Falling back to sequential mode.\n"
                )
                all_results.clear()
                run_sequential()
        else:
            run_sequential()
    except Exception as exc:
        print(f"\nERROR: {exc}")
        print("Hint: check that the network file is valid and both sumo and duarouter are on PATH.")
        raise SystemExit(1)

    all_results.sort(
        key=lambda result: result.get("baseline", {}).get("run", math.inf)
    )

    # --- Save summary ---
    summary = generate_summary(all_results, closed_edge, plans)
    summary["metadata"].update(
        {
            "trips_file": str(trips_file),
            "network_file": str(args.net.resolve()),
            "simulation_window_s": {"begin": sim_begin, "end": sim_end},
            "random_factor": args.random_factor,
            "parallel_workers": workers,
        }
    )
    summary_file = args.output / "summary.json"
    summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"\n{SEP}")
    print(f"DONE  — output: {args.output}")
    print(f"       summary: {summary_file}")
    print(SEP)


if __name__ == "__main__":
    main()
