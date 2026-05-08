"""
report.py
---------
Markdown report generation for Monte Carlo evaluation results.

Responsibility: transform the evaluation dict produced by evaluate.py into a
human-readable Markdown document.  All formatting logic lives here; no
statistical computation is performed.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from evaluator.decision import _weights_are_equal


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt(value: Optional[float], digits: int = 4) -> str:
    """Format a float to digits decimal places, or return n/a."""
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _section_primary_endpoint(paired_delay: Dict[str, Any]) -> list[str]:
    lines = [
        "## Primary Endpoint: Paired Time-Loss Delay Increase vs Baseline",
        "",
        "| Plan | Mean Δ time-loss delay (vh) | 95 % CI | Paired t-test p | Wilcoxon p |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for plan_id, payload in paired_delay.items():
        s = payload["summary"]
        t = payload["tests_against_zero"]
        lines.append(
            f"| {plan_id} "
            f"| {fmt(s['mean'], 4)} "
            f"| [{fmt(s['ci95_low'], 4)}, {fmt(s['ci95_high'], 4)}] "
            f"| {fmt(t['ttest']['pvalue'], 6)} "
            f"| {fmt(t['wilcoxon']['pvalue'], 6)} |"
        )
    return lines


def _section_secondary_endpoint(paired_travel_time: Dict[str, Any]) -> list[str]:
    lines = [
        "## Secondary Endpoint: Paired Travel Time Increase vs Baseline",
        "",
        "| Plan | Mean Δ travel time (min) | 95 % CI | Paired t-test p | Wilcoxon p |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for plan_id, payload in paired_travel_time.items():
        s = payload["summary"]
        t = payload["tests_against_zero"]
        lines.append(
            f"| {plan_id} "
            f"| {fmt(s['mean'], 4)} "
            f"| [{fmt(s['ci95_low'], 4)}, {fmt(s['ci95_high'], 4)}] "
            f"| {fmt(t['ttest']['pvalue'], 6)} "
            f"| {fmt(t['wilcoxon']['pvalue'], 6)} |"
        )
    return lines


def _section_pareto(decision: Dict[str, Any]) -> list[str]:
    front = decision["pareto_front"]
    dominated = decision["dominated_plans"]
    return [
        "## Pareto Screening",
        "",
        "Pareto screening is applied before weighting so that no dominated plan "
        "can be rescued by a favourable weight choice.",
        "",
        f"- Pareto-efficient plans: "
        f"{', '.join(f'`{pid}`' for pid in front) if front else 'none'}",
        f"- Pareto-dominated plans: "
        f"{', '.join(f'`{pid}`' for pid in dominated) if dominated else 'none'}",
    ]


def _section_principal_ranking(decision: Dict[str, Any]) -> list[str]:
    w = decision["reference_weights"]
    if _weights_are_equal(w):
        weight_note = (
            "Equal reference weights are used as the principal no-preference "
            "baseline; see the Weight Robustness section for sensitivity results."
        )
    else:
        weight_note = (
            "The principal ranking uses the configured reference weights below; "
            "see the Weight Robustness section for sensitivity results."
        )
    lines = [
        "## Principal Decision Ranking",
        "",
        weight_note,
        "",
        f"- Reference weights: "
        f"time_loss={fmt(w['mean_delta_delay_vh'], 4)}, "
        f"travel_time={fmt(w['mean_delta_travel_time_min'], 4)}, "
        f"phi_net={fmt(w['mean_phi_net'], 4)}",
        "",
        "| Plan | Feasible | Pareto dominated | Mean Δ time-loss delay (vh) | "
        "Mean Δ travel time (min) | Mean phi_net | Score |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for row in decision["ranking"]:
        if not row["feasible"]:
            score_str = "rejected"
        elif row["pareto_dominated"]:
            score_str = "screened_out"
        else:
            score_str = fmt(row["score"], 4)
        lines.append(
            f"| {row['plan_id']} "
            f"| {'yes' if row['feasible'] else 'no'} "
            f"| {'yes' if row['pareto_dominated'] else 'no'} "
            f"| {fmt(row['mean_delta_delay_vh'], 4)} "
            f"| {fmt(row['mean_delta_travel_time_min'], 4)} "
            f"| {fmt(row['mean_phi_net'], 4)} "
            f"| {score_str} |"
        )
    return lines


def _section_weight_robustness(decision: Dict[str, Any]) -> list[str]:
    robustness = decision["weight_robustness"]
    lines = [
        "## Weight Robustness",
        "",
        f"Weight vectors were sampled uniformly from the {len(decision['criteria'])}-criterion "
        f"weight simplex using a Dirichlet(1, …, 1) distribution "
        f"({robustness['samples']:,} samples, seed {robustness['seed']}).  "
        "A plan that ranks first under a large fraction of weight vectors is "
        "less sensitive to the principal weight choice.",
        "",
        "| Plan | P(rank 1) | Mean rank | Score q05 | Score median | Score q95 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for plan_id in decision["pareto_front"]:
        q = robustness["score_quantiles"][plan_id]
        lines.append(
            f"| {plan_id} "
            f"| {fmt(robustness['top_1_frequency'][plan_id], 4)} "
            f"| {fmt(robustness['mean_rank'][plan_id], 4)} "
            f"| {fmt(q['q05'], 4)} "
            f"| {fmt(q['q50'], 4)} "
            f"| {fmt(q['q95'], 4)} |"
        )
    return lines


def _section_feasibility(feasibility: Dict[str, Any]) -> list[str]:
    lines = ["## Feasibility", ""]
    for plan_id, payload in feasibility.items():
        status = "OK" if payload["all_runs_meet_baseline_throughput"] else "Violation"
        lines.append(f"- `{plan_id}`: {status}")
        if payload["violations"]:
            runs = ", ".join(str(row["run"]) for row in payload["violations"])
            lines.append(f"  Throughput dropped below baseline in runs: {runs}")
    return lines


def _section_impacted_vehicles(impacted: Dict[str, Any]) -> list[str]:
    lines = [
        "## Impacted-Vehicle Appendix",
        "",
        "| Plan | Mean delta_t_min | Mean phi_net | Mean v_net_kmh | Mean vehicles_compared |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for plan_id, payload in impacted.items():
        m = payload["metrics"]
        lines.append(
            f"| {plan_id} "
            f"| {fmt(m['delta_t_min']['mean'], 4)} "
            f"| {fmt(m['phi_net']['mean'], 4)} "
            f"| {fmt(m['v_net_kmh']['mean'], 4)} "
            f"| {fmt(m['vehicles_compared']['mean'], 1)} |"
        )
    return lines


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def build_markdown_report(evaluation: Dict[str, Any]) -> str:
    """Assemble a complete Markdown evaluation report.

    Parameters
    ----------
    evaluation:
        The top-level evaluation dict written by evaluate.py.

    Returns
    -------
    A single Markdown string terminated by a newline.
    """
    meta = evaluation["metadata"]
    decision = evaluation["decision"]

    header = [
        "# Monte Carlo Evaluation",
        "",
        f"- Input directory: `{meta['input_dir']}`",
        f"- Closed edge: `{meta.get('closed_edge', 'unknown')}`",
        f"- Runs analysed: `{meta['total_runs']}`",
        (
            f"- Incomplete runs excluded: `{len(meta['excluded_incomplete_runs'])}`"
            if meta.get("excluded_incomplete_runs")
            else "- Incomplete runs excluded: `0`"
        ),
        f"- Time-loss metric: `{meta.get('delay_metric', 'sum_sumo_timeLoss_completed_trips_vh')}`",
        f"- Best plan under the principal decision rule: `{decision['best_plan']}`",
        f"- Decision method: `{decision.get('method', 'n/a')}`",
    ]

    if meta.get("excluded_incomplete_runs"):
        header.append(
            "- Excluded run details: "
            + "; ".join(
                f"`run_{row['run']}` missing {', '.join(row['missing_result_keys'])}"
                for row in meta["excluded_incomplete_runs"]
            )
        )

    sections = [
        header,
        _section_primary_endpoint(evaluation["paired_delay_vh"]),
        _section_secondary_endpoint(evaluation["paired_travel_time_min"]),
        _section_pareto(decision),
        _section_principal_ranking(decision),
        _section_weight_robustness(decision),
        _section_feasibility(evaluation["feasibility"]),
        _section_impacted_vehicles(evaluation["impacted_vehicle_analysis"]["per_plan"]),
    ]

    # Join sections with a blank line between each.
    parts = []
    for section in sections:
        parts.append("\n".join(section))

    return "\n\n".join(parts) + "\n"
