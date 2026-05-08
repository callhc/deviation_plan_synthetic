"""
decision.py
-----------
Multi-criteria decision analysis (MCDA) for Monte Carlo detour-plan evaluation.

The decision pipeline has three stages:

1. Feasibility gate: reject plans whose completed-trip count falls below
   the baseline in any Monte Carlo run.
2. Pareto screening: among feasible plans, eliminate those dominated on all
   three efficiency criteria simultaneously.
3. Additive weighted scoring: rank Pareto-efficient survivors with a
   normalised linear additive value function, then test robustness via Monte
   Carlo weight sampling on the weight simplex.

Reference weights
-----------------
All three criteria are pure efficiency metrics (no equity dimension):

* mean_delta_delay_vh: aggregate vehicle-hours of additional SUMO timeLoss
  across the whole network.
* mean_delta_travel_time_min: average per-vehicle travel-time increase.
* mean_phi_net: net impact score on directly affected vehicles.

REFERENCE_WEIGHTS defines the principal additive scoring rule.  The report
describes the actual configured values rather than assuming a fixed scheme.  If
the configured vector is uniform, it can be interpreted as a no-preference
baseline; if not, it should be interpreted as an explicit modelling choice.
The weight-robustness analysis then tests whether the ranking remains stable
across all admissible weight vectors,
making the specific reference vector less consequential when robustness is high.
"""

from __future__ import annotations

import math
import random
import statistics
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Reference weights used by the principal additive score.
# ---------------------------------------------------------------------------

REFERENCE_WEIGHTS: Dict[str, float] = {
    "mean_delta_delay_vh": 1 / 3,
    "mean_delta_travel_time_min": 1 / 3,
    "mean_phi_net": 1 / 3,
}

# Number of weight-simplex samples used in the robustness analysis.
WEIGHT_ROBUSTNESS_SAMPLES: int = 20_000

# Criteria used throughout (order determines tie-breaking in sorting).
_CRITERIA: List[str] = [
    "mean_delta_delay_vh",
    "mean_delta_travel_time_min",
    "mean_phi_net",
]


def _weights_are_equal(weights: Dict[str, float], tol: float = 1e-12) -> bool:
    """Return True when all criteria weights are numerically equal."""
    values = list(weights.values())
    return max(values) - min(values) <= tol


def _format_weight_vector(weights: Dict[str, float]) -> str:
    """Format the configured reference-weight vector for human-readable output."""
    return (
        f"time_loss={weights['mean_delta_delay_vh']:.4f}, "
        f"travel_time={weights['mean_delta_travel_time_min']:.4f}, "
        f"phi_net={weights['mean_phi_net']:.4f}"
    )


# ---------------------------------------------------------------------------
# Feasibility
# ---------------------------------------------------------------------------

def collect_feasibility(
    run_results: List[Dict[str, Any]],
    plan_ids: List[str],
) -> Dict[str, Any]:
    """Identify plans that reduce completed trips below the baseline.

    A plan is infeasible if there exists at least one Monte Carlo run where
    its completed-trip count is strictly lower than the baseline's.  Throughput
    preservation is treated as a hard constraint: no efficiency gain can
    compensate for a loss of served demand.

    Parameters
    ----------
    run_results:
        List of per-run result dicts.
    plan_ids:
        Non-baseline plan identifiers to evaluate.

    Returns
    -------
    dict mapping each plan_id to all_runs_meet_baseline_throughput (bool)
    and violations (list of offending run records).
    """
    feasibility: Dict[str, Any] = {}
    for plan_id in plan_ids:
        violations = []
        for result in run_results:
            baseline = result.get("baseline")
            plan = result.get(plan_id)
            if not baseline or not plan:
                continue
            if int(plan["completed"]) < int(baseline["completed"]):
                violations.append(
                    {
                        "run": int(plan["run"]),
                        "baseline_completed": int(baseline["completed"]),
                        "plan_completed": int(plan["completed"]),
                    }
                )
        feasibility[plan_id] = {
            "all_runs_meet_baseline_throughput": not violations,
            "violations": violations,
        }
    return feasibility


# ---------------------------------------------------------------------------
# Pareto screening
# ---------------------------------------------------------------------------

def _dominates(row_a: Dict[str, Any], row_b: Dict[str, Any]) -> bool:
    """Return True if row_a Pareto-dominates row_b on all three criteria.

    Dominance requires row_a to be no worse than row_b on every criterion and
    strictly better on at least one.  All three criteria are costs (lower is
    better).
    """
    no_worse = all(
        row_a[c] <= row_b[c] for c in _CRITERIA
    )
    strictly_better = any(
        row_a[c] < row_b[c] for c in _CRITERIA
    )
    return no_worse and strictly_better


def apply_pareto_screening(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Flag dominated plans among feasible candidates and return the Pareto front.

    Pareto screening is applied before weighting so that the choice of
    reference weights cannot accidentally retain a dominated alternative.

    Parameters
    ----------
    rows:
        Decision rows produced by _build_decision_rows; modified in-place.

    Returns
    -------
    dict with pareto_front and dominated_plans (lists of plan_id strings).
    """
    feasible = [row for row in rows if row["feasible"]]

    # Reset flags before (re-)computing to allow idempotent calls.
    for row in feasible:
        row["pareto_dominated"] = False
        row["dominates"] = []
        row["dominated_by"] = []

    for row in feasible:
        for other in feasible:
            if row["plan_id"] == other["plan_id"]:
                continue
            if _dominates(other, row):
                row["pareto_dominated"] = True
                row["dominated_by"].append(other["plan_id"])
            if _dominates(row, other):
                row["dominates"].append(other["plan_id"])

    pareto_front = [row["plan_id"] for row in feasible if not row["pareto_dominated"]]
    dominated = [row["plan_id"] for row in feasible if row["pareto_dominated"]]
    return {"pareto_front": pareto_front, "dominated_plans": dominated}


# ---------------------------------------------------------------------------
# Additive scoring
# ---------------------------------------------------------------------------

def _normalize(value: float, lower: float, upper: float) -> float:
    """Min-max normalise value to [0, 1].

    Returns 0.0 when lower == upper (all plans identical on this criterion).
    """
    if math.isclose(lower, upper):
        return 0.0
    return (value - lower) / (upper - lower)


def compute_additive_scores(
    rows: List[Dict[str, Any]],
    survivor_ids: List[str],
    weights: Dict[str, float],
) -> List[Dict[str, Any]]:
    """Score and rank Pareto-efficient plans with a normalised additive value function.

    Each criterion is min-max normalised across the survivor set so that the
    worst plan on each dimension scores 1 and the best scores 0 (lower is
    better).  The composite score is a weighted sum of normalised costs.

    Parameters
    ----------
    rows:
        Full list of decision rows; non-survivors are ignored.
    survivor_ids:
        Plan IDs on the Pareto front.
    weights:
        Dict mapping criterion name → weight; weights should sum to 1.

    Returns
    -------
    Survivor rows sorted by ascending score (best plan first), with
    score and normalized_components fields populated.
    """
    survivors = [row for row in rows if row["plan_id"] in survivor_ids]
    if not survivors:
        return []

    # Pre-compute per-criterion bounds across survivors.
    bounds = {
        c: (
            min(row[c] for row in survivors),
            max(row[c] for row in survivors),
        )
        for c in _CRITERIA
    }

    for row in survivors:
        components = {
            c: _normalize(row[c], bounds[c][0], bounds[c][1])
            for c in _CRITERIA
        }
        row["normalized_components"] = {
            "time_loss_cost": components["mean_delta_delay_vh"],
            "travel_time_cost": components["mean_delta_travel_time_min"],
            "phi_cost": components["mean_phi_net"],
        }
        row["score"] = sum(weights[c] * components[c] for c in _CRITERIA)

    survivors.sort(
        key=lambda row: (
            row["score"],
            row["mean_delta_delay_vh"],
            row["mean_delta_travel_time_min"],
            row["mean_phi_net"],
            row["plan_id"],
        )
    )
    return survivors


# ---------------------------------------------------------------------------
# Weight robustness / sensitivity analysis
# ---------------------------------------------------------------------------

def _sample_dirichlet_weights(
    rng: random.Random,
    criteria_keys: List[str],
) -> Dict[str, float]:
    """Draw a weight vector uniformly from the criteria weight simplex.

    Uses the Dirichlet(1, …, 1) distribution — equivalent to a uniform
    distribution over the simplex — via the gamma-variate method.

    Parameters
    ----------
    rng:
        A seeded random.Random instance for reproducibility.
    criteria_keys:
        Ordered list of criterion names; determines vector length.
    """
    draws = [rng.gammavariate(1.0, 1.0) for _ in criteria_keys]
    total = sum(draws) or 1.0
    return {key: draw / total for key, draw in zip(criteria_keys, draws)}


def _score_quantiles(values: List[float]) -> Dict[str, float]:
    """Return q05, median, q95 and mean of values.

    Uses explicit index arithmetic so that the intent is unambiguous:
    q05 = the value at the 5th percentile, q95 = the 95th percentile.
    """
    if not values:
        return {"q05": 0.0, "q50": 0.0, "q95": 0.0, "mean": 0.0}
    ordered = sorted(values)
    n = len(ordered)
    # Linear interpolation indices for 5th and 95th percentiles.
    idx_05 = (n - 1) * 0.05
    idx_95 = (n - 1) * 0.95
    lo_05, hi_05 = int(idx_05), min(int(idx_05) + 1, n - 1)
    lo_95, hi_95 = int(idx_95), min(int(idx_95) + 1, n - 1)
    frac_05 = idx_05 - lo_05
    frac_95 = idx_95 - lo_95
    q05 = ordered[lo_05] + frac_05 * (ordered[hi_05] - ordered[lo_05])
    q95 = ordered[lo_95] + frac_95 * (ordered[hi_95] - ordered[lo_95])
    return {
        "q05": q05,
        "q50": statistics.median(ordered),
        "q95": q95,
        "mean": statistics.mean(values),
    }


def compute_weight_robustness(
    rows: List[Dict[str, Any]],
    survivor_ids: List[str],
    samples: int = WEIGHT_ROBUSTNESS_SAMPLES,
    seed: int = 42,
) -> Dict[str, Any]:
    """Assess ranking stability by Monte Carlo sampling over the weight simplex.

    Rather than committing to a single weight vector, this analysis asks:
    "Across all defensible weight allocations, how often does each plan rank
    first?"  A plan that ranks first under the vast majority of weight vectors
    is preferred irrespective of the exact weight choice, which greatly
    strengthens the recommendation.

    Weight vectors are drawn from a Dirichlet(1,1,1) distribution — the
    uniform distribution over the 2-simplex — so no particular trade-off
    preference is favoured.

    Note on ties: when two plans share an identical score for a given weight
    draw, the winner is determined by the same lexicographic tie-break used in
    compute_additive_scores (time loss → travel time → phi_net → plan_id).
    Ties are not split probabilistically; this is conservative and consistent.

    Parameters
    ----------
    rows:
        Full decision-row list; non-survivors are ignored.
    survivor_ids:
        Pareto-front plan IDs to include in the analysis.
    samples:
        Number of weight vectors to sample.  Default 20 000 gives stable
        frequency estimates (SE < 0.4 % for probabilities near 0.5).
    seed:
        Random seed for reproducibility.

    Returns
    -------
    dict with:
    - top_1_frequency: fraction of samples each plan ranked first.
    - mean_rank: average rank across all samples.
    - rank_acceptability: full rank-frequency distribution per plan.
    - score_quantiles: q05, median, q95 and mean of composite scores.
    - pairwise_win_probability: P(plan A scores lower than plan B).
    """
    survivors = [row for row in rows if row["plan_id"] in survivor_ids]
    if not survivors:
        return {
            "samples": 0,
            "distribution": "uniform_dirichlet_on_simplex",
            "seed": seed,
            "top_1_frequency": {},
            "mean_rank": {},
            "rank_acceptability": {},
            "score_quantiles": {},
            "pairwise_win_probability": {},
        }

    rng = random.Random(seed)

    # Pre-normalise criterion values once (bounds fixed across all weight draws).
    bounds = {
        c: (
            min(row[c] for row in survivors),
            max(row[c] for row in survivors),
        )
        for c in _CRITERIA
    }
    normalised: Dict[str, Dict[str, float]] = {
        row["plan_id"]: {
            c: _normalize(row[c], bounds[c][0], bounds[c][1])
            for c in _CRITERIA
        }
        for row in survivors
    }

    # Accumulators.
    plan_ids = [row["plan_id"] for row in survivors]
    top_1_counts = {pid: 0 for pid in plan_ids}
    rank_counts = {pid: {r: 0 for r in range(1, len(survivors) + 1)} for pid in plan_ids}
    score_samples: Dict[str, List[float]] = {pid: [] for pid in plan_ids}
    pairwise_wins: Dict[str, Dict[str, int]] = {
        pid: {other: 0 for other in plan_ids if other != pid}
        for pid in plan_ids
    }

    for _ in range(samples):
        w = _sample_dirichlet_weights(rng, _CRITERIA)
        scored = []
        for row in survivors:
            pid = row["plan_id"]
            score = sum(w[c] * normalised[pid][c] for c in _CRITERIA)
            score_samples[pid].append(score)
            scored.append(
                {
                    "plan_id": pid,
                    "score": score,
                    # Lexicographic tie-break mirrors compute_additive_scores.
                    "time_loss": row["mean_delta_delay_vh"],
                    "travel_time": row["mean_delta_travel_time_min"],
                    "phi": row["mean_phi_net"],
                }
            )

        scored.sort(
            key=lambda item: (
                item["score"],
                item["time_loss"],
                item["travel_time"],
                item["phi"],
                item["plan_id"],
            )
        )
        top_1_counts[scored[0]["plan_id"]] += 1
        for rank, item in enumerate(scored, start=1):
            rank_counts[item["plan_id"]][rank] += 1

        # Pairwise wins: winner strictly scores lower than loser.
        for i, winner in enumerate(scored):
            for loser in scored[i + 1:]:
                if winner["score"] < loser["score"]:
                    pairwise_wins[winner["plan_id"]][loser["plan_id"]] += 1

    return {
        "samples": samples,
        "distribution": "uniform_dirichlet_on_simplex",
        "seed": seed,
        "top_1_frequency": {
            pid: top_1_counts[pid] / samples for pid in plan_ids
        },
        "mean_rank": {
            pid: sum(r * cnt for r, cnt in rank_counts[pid].items()) / samples
            for pid in plan_ids
        },
        "rank_acceptability": {
            pid: {str(r): cnt / samples for r, cnt in rank_counts[pid].items()}
            for pid in plan_ids
        },
        "score_quantiles": {
            pid: _score_quantiles(score_samples[pid]) for pid in plan_ids
        },
        "pairwise_win_probability": {
            pid: {other: wins / samples for other, wins in others.items()}
            for pid, others in pairwise_wins.items()
        },
    }


# ---------------------------------------------------------------------------
# Top-level decision function
# ---------------------------------------------------------------------------

def _build_decision_rows(
    paired_delay: Dict[str, Any],
    paired_travel_time: Dict[str, Any],
    impacted_vehicle_analysis: Dict[str, Any],
    feasibility: Dict[str, Any],
    plan_ids: List[str],
) -> List[Dict[str, Any]]:
    """Assemble one decision row per plan from the three metric analyses."""
    rows = []
    for plan_id in plan_ids:
        impacted = impacted_vehicle_analysis["per_plan"][plan_id]["metrics"]
        rows.append(
            {
                "plan_id": plan_id,
                "feasible": feasibility[plan_id]["all_runs_meet_baseline_throughput"],
                "mean_delta_delay_vh": paired_delay[plan_id]["summary"]["mean"],
                "mean_delta_travel_time_min": paired_travel_time[plan_id]["summary"]["mean"],
                "mean_phi_net": impacted["phi_net"]["mean"],
                # Fields populated by downstream functions:
                "pareto_dominated": False,
                "dominates": [],
                "dominated_by": [],
                "score": None,
                "normalized_components": None,
            }
        )
    return rows


def choose_best_plan_multicriteria(
    paired_delay: Dict[str, Any],
    paired_travel_time: Dict[str, Any],
    impacted_vehicle_analysis: Dict[str, Any],
    feasibility: Dict[str, Any],
    plan_ids: List[str],
) -> Dict[str, Any]:
    """Run the full MCDA pipeline and return a ranked decision report.

    Pipeline stages
    ---------------
    1. Build one decision row per plan.
    2. Reject infeasible plans (throughput hard constraint).
    3. Eliminate Pareto-dominated feasible plans.
    4. Score and rank survivors with the equal reference weights.
    5. Test ranking robustness across the full weight simplex.

    Returns
    -------
    dict containing best_plan, full ranking, Pareto metadata, weight
    robustness results, and documentation of the decision rule and method.
    """
    rows = _build_decision_rows(
        paired_delay,
        paired_travel_time,
        impacted_vehicle_analysis,
        feasibility,
        plan_ids,
    )
    pareto = apply_pareto_screening(rows)
    principal_ranking = compute_additive_scores(rows, pareto["pareto_front"], REFERENCE_WEIGHTS)
    robustness = compute_weight_robustness(rows, pareto["pareto_front"])
    best_plan = principal_ranking[0]["plan_id"] if principal_ranking else None

    infeasible_rows = [row for row in rows if not row["feasible"]]
    dominated_rows = [row for row in rows if row["feasible"] and row["pareto_dominated"]]
    report_ranking = principal_ranking + dominated_rows + infeasible_rows

    return {
        "best_plan": best_plan,
        "ranking": report_ranking,
        "principal_ranking": principal_ranking,
        "reference_weights": REFERENCE_WEIGHTS,
        "pareto_front": pareto["pareto_front"],
        "dominated_plans": pareto["dominated_plans"],
        "weight_robustness": robustness,
        "decision_rule": [
            "Reject plans that reduce completed trips relative to baseline.",
            "Among feasible plans, eliminate Pareto-dominated alternatives.",
            "Rank Pareto-efficient plans with an additive weighted normalised score "
            f"(reference weights: {_format_weight_vector(REFERENCE_WEIGHTS)}"
            + (
                "; equal-weight no-preference baseline)."
                if _weights_are_equal(REFERENCE_WEIGHTS)
                else ")."
            ),
            "Test robustness by Monte Carlo sampling of weight vectors uniformly "
            "over the weight simplex (Dirichlet(1,1,1), N=20 000).",
        ],
        "criteria": _CRITERIA,
        "method": "pareto_additive_weighted_score_with_dirichlet_robustness",
    }
