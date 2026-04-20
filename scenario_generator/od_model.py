"""TAZ-level OD modelling for the scenario generator.

Pipeline role: Stage 1 : Demand Generation.

This module builds the hourly Origin-Destination (OD) matrices that drive all
downstream vehicle instantiation.  The three sub-tasks are:

1. Temporal profile : a normalized two-Gaussian curve converts a scalar
   daily demand into hour-by-hour totals (Equation :eq:`twopeak`).
2. Spatial distribution : a singly-constrained gravity model allocates
   demand across TAZ pairs.
3. Commute overlay : an optional directional bias amplifies residential →
   work flows in the AM peak and the reverse in the PM peak.

All three sub-tasks operate on share matrices (non-negative, sum to 1) that
are later scaled to exact integer trip counts via largest-remainder rounding.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping, Sequence

import numpy as np
import pandas as pd

from scenario_generator.zones import TrafficZone


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ZoneRoleWeights:
    """Residential and work intensity assigned to one TAZ.

    Values are relative weights, not probabilities : a mixed-use zone can
    therefore contribute to both home->work and work->home commute flows.
    """

    residential: float = 0.0
    work: float = 0.0


@dataclass
class CommutePattern:
    """Directional commute structure layered on top of background OD demand.

    The overlay is blended into the base gravity matrix hour-by-hour using a
    Gaussian kernel centred on each peak hour (see 'build_hourly_od_shares').
    """

    zone_roles: dict[str, ZoneRoleWeights]
    peak_weight: float = 2.5
    commute_distance_decay_beta: float = 0.0
    morning_peak_hour: int = 8
    evening_peak_hour: int = 17
    sigma_hours: float = 2.0


# ---------------------------------------------------------------------------
# Temporal demand profile
# ---------------------------------------------------------------------------

def gaussian_profile(hours: int, peak_hour: int, sigma: float) -> np.ndarray:
    """Return a unit-height Gaussian intensity curve over hours time steps.

    Parameters
    ----------
    hours:
        Number of discrete hours (typically 24).
    peak_hour:
        Hour index at which the Gaussian reaches its maximum.
    sigma:
        Standard deviation in hours controlling spread.
    """
    if sigma <= 0.0:
        raise ValueError("Gaussian sigma must be strictly positive.")
    h = np.arange(hours)
    return np.exp(-0.5 * ((h - peak_hour) / sigma) ** 2)


def generate_two_peak_profile(
    hours: int = 24,
    morning_peak: int = 8,
    evening_peak: int = 17,
    morning_amp: float = 1.0,
    evening_amp: float = 1.2,
    sigma: float = 2.5,
) -> np.ndarray:
    """Generate a normalized two-Gaussian daily demand profile.

    The profile is a weighted sum of two Gaussian kernels:

    The result sums to 1 and is used to distribute daily demand across hours.
    The slightly higher evening amplitude (default 1.2 vs 1.0) reflects the
    empirically observed asymmetry between AM and PM peaks.
    """
    profile = (
        morning_amp * gaussian_profile(hours, morning_peak, sigma)
        + evening_amp * gaussian_profile(hours, evening_peak, sigma)
    )
    profile /= profile.sum()
    return profile


def allocate_hourly_totals(
    profile: np.ndarray,
    total_daily_demand: int,
    min_per_hour: int = 0,
) -> np.ndarray:
    """Convert a normalized profile into exact integer hourly totals.

    The standard largest-remainder method is applied: each hour receives
    floor(N * p_h) trips, and the leftover trips (so that the total is
    exactly *total_daily_demand*) are handed to the hours with the largest
    fractional remainders.  The resulting integer profile preserves the
    continuous profile proportions as closely as possible.

    Parameters
    ----------
    profile:
        Normalised hourly profile (sums to 1).
    total_daily_demand:
        Total number of trips *N* to allocate.
    min_per_hour:
        Optional floor applied *before* the largest-remainder allocation.
        Defaults to 0 (pure largest-remainder, matching the methodology).
        A strictly positive value guarantees at least that many trips per
        slot at the cost of a mild distortion of the profile proportions.
    """
    nb_hours = len(profile)
    total_daily_demand = int(total_daily_demand)

    if min_per_hour < 0:
        raise ValueError("min_per_hour must be non-negative.")
    if total_daily_demand < min_per_hour * nb_hours:
        raise ValueError(
            f"Cannot guarantee at least {min_per_hour} trip(s) per hour with "
            f"{total_daily_demand} trips over {nb_hours} hours."
        )

    hourly_totals = np.full(nb_hours, min_per_hour, dtype=int)
    remaining = total_daily_demand - (min_per_hour * nb_hours)
    if remaining <= 0:
        return hourly_totals

    scaled = np.asarray(profile, dtype=float)
    scaled /= scaled.sum()
    fractional = remaining * scaled
    increments = np.floor(fractional).astype(int)
    hourly_totals += increments

    leftovers = remaining - int(increments.sum())
    if leftovers > 0:
        remainders = fractional - increments
        order = np.argsort(-remainders, kind="stable")
        hourly_totals[order[:leftovers]] += 1

    return hourly_totals


def allocate_integer_matrix(prob_matrix: pd.DataFrame, total_count: int) -> pd.DataFrame:
    """Allocate total_count trips across OD cells using largest-remainder rounding.

    The function preserves the relative proportions of *prob_matrix* while
    guaranteeing that the cell counts are non-negative integers that sum
    exactly to total_count.
    """
    if total_count <= 0:
        return pd.DataFrame(0, index=prob_matrix.index, columns=prob_matrix.columns, dtype=int)

    flat_probs = prob_matrix.to_numpy(dtype=float).reshape(-1)
    prob_sum = flat_probs.sum()
    if prob_sum <= 0:
        raise ValueError("OD probability matrix has no positive mass to allocate demand.")

    flat_probs /= prob_sum
    expected = flat_probs * int(total_count)
    counts = np.floor(expected).astype(int)

    leftovers = int(total_count) - int(counts.sum())
    if leftovers > 0:
        remainders = expected - counts
        order = np.argsort(-remainders, kind="stable")
        counts[order[:leftovers]] += 1

    shaped = counts.reshape(prob_matrix.shape)
    return pd.DataFrame(shaped, index=prob_matrix.index, columns=prob_matrix.columns, dtype=int)


# ---------------------------------------------------------------------------
# Gravity OD model
# ---------------------------------------------------------------------------

def build_gravity_od_matrix(
    zones: Mapping[str, TrafficZone],
    impedance_matrix: pd.DataFrame,
    beta: float,
) -> pd.DataFrame:
    """Build a normalized daily OD share matrix using a singly-constrained gravity model.

    The unnormalised gravity weight between origin *i* and destination *j* is

        w_{ij} = m_i * m_j * e^{-\beta c_{ij}}

    where m_i is the zone mass and c_{ij} is the impedance.
    The model is singly-constrained on the production side: the total trip
    production of each zone is fixed to its normalised mass
    O_i = m_i / SUM_k m_k, while destinations follow from the
    distance-decayed attractiveness of the remaining zones.  Concretely, the
    share of the OD pair (i, j) is
        S_{ij} = (m_i / SUM_k m_k) * (m_j * e^{-\beta c_{ij}} / SUM_l m_l * e^{-\beta c_{il}}),

    so that SUM_j S_{ij} = m_i / SUM_k m_k and
    SUM_{ij} S_{ij} = 1 (whenever every origin has at least one
    reachable destination; otherwise the matrix is renormalised over the
    active origins).

    Parameters
    ----------
    zones:
        TAZ registry keyed by zone id.
    impedance_matrix:
        Square DataFrame of TAZ-to-TAZ travel costs (metres).
    beta:
        Distance-decay parameter; larger values concentrate demand on
        shorter trips.
    """
    zone_ids = list(zones.keys())
    mass = np.array([zones[z].mass for z in zone_ids], dtype=float)
    imp = impedance_matrix.reindex(index=zone_ids, columns=zone_ids).to_numpy(dtype=float)

    # Unnormalised gravity weights: w_ij = m_i * m_j * exp(-beta * c_ij)
    if beta == 0.0:
        decay = np.ones_like(imp)
    else:
        decay = np.exp(-beta * imp)

    W = np.outer(mass, mass) * decay
    np.fill_diagonal(W, 0.0)                          # no intra-zonal demand
    W[~np.isfinite(imp) | (imp <= 0.0)] = 0.0         # mask unreachable pairs

    # Row-normalise: conditional destination probability p(j|i).
    # The m_i factor in w_ij cancels out, leaving
    #   p(j|i) = m_j exp(-beta c_ij) / sum_l m_l exp(-beta c_il).
    row_sums = W.sum(axis=1, keepdims=True)
    with np.errstate(invalid="ignore", divide="ignore"):
        cond_prob = np.where(row_sums > 0.0, W / row_sums, 0.0)

    # Origin productions proportional to zone mass: O_i = m_i / sum_k m_k.
    total_mass = float(mass.sum())
    if total_mass <= 0.0:
        raise ValueError("Could not derive a valid gravity OD matrix: total zone mass is zero.")
    origin_share = mass / total_mass

    # Joint share: S_ij = O_i * p(j|i).
    shares = origin_share[:, np.newaxis] * cond_prob

    total = float(shares.sum())
    if total <= 0.0:
        raise ValueError("Could not derive a valid gravity OD matrix from the TAZ network.")
    # Renormalise to sum to 1 (no-op when every origin has at least one
    # reachable destination; otherwise reallocates the dropped mass).
    shares = shares / total

    return pd.DataFrame(shares, index=zone_ids, columns=zone_ids)


def build_commute_od_matrices(
    zones: Mapping[str, TrafficZone],
    impedance_matrix: pd.DataFrame,
    commute_pattern: CommutePattern,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build directional commute OD shares for AM and PM peaks.

    The morning (residential → work) and evening (work → residential) share
    matrices are computed as gravity-weighted flows with directional factors:

        w^{AM}_{ij} = m_i m_j * f(c_{ij})
                                * r_i * e_j

        w^{PM}_{ij} = m_i m_j * f(c_{ij})
                                * e_i * r_j

    where r_i and e_i are the residential and employment
    (work) weights of zone i, and f(c_{ij}) = e^{-beta c_{ij}}.

    Both matrices are normalized to sum to 1 (OD shares).  They are blended
    into the base gravity matrix hour-by-hour in build_hourly_od_shares.
    """
    zone_ids = list(zones.keys())
    mass = np.array([zones[z].mass for z in zone_ids], dtype=float)
    roles = [commute_pattern.zone_roles.get(z, ZoneRoleWeights()) for z in zone_ids]
    residential = np.array([r.residential for r in roles], dtype=float)
    work = np.array([r.work for r in roles], dtype=float)

    imp = impedance_matrix.reindex(index=zone_ids, columns=zone_ids).to_numpy(dtype=float)
    beta = float(commute_pattern.commute_distance_decay_beta)

    if beta == 0.0:
        decay = np.ones_like(imp)
    else:
        decay = np.exp(-beta * imp)

    # Mask invalid cells and diagonal
    valid = np.isfinite(imp) & (imp > 0.0)
    decay[~valid] = 0.0
    np.fill_diagonal(decay, 0.0)

    base = np.outer(mass, mass) * decay

    morning = base * np.outer(residential, work)      # home → work
    evening = base * np.outer(work, residential)      # work → home

    return (
        _normalize_positive_matrix(pd.DataFrame(morning, index=zone_ids, columns=zone_ids)),
        _normalize_positive_matrix(pd.DataFrame(evening, index=zone_ids, columns=zone_ids)),
    )


# ---------------------------------------------------------------------------
# Hourly OD share assembly
# ---------------------------------------------------------------------------

def build_hourly_od_shares(
    base_od_share: pd.DataFrame,
    *,
    nb_hours: int,
    commute_pattern: CommutePattern | None = None,
    commute_morning_share: pd.DataFrame | None = None,
    commute_evening_share: pd.DataFrame | None = None,
) -> Dict[int, pd.DataFrame]:
    """Blend the base gravity share with the commute overlay for each hour.

    Without a commute pattern the base share is used unchanged for all hours.
    With a commute pattern, the blended share for hour *h* is:

        S^h_{ij} propto S^{base}_{ij}
            + lambda * g_h(mu_m, sigma) * S^{AM}_{ij}
            + lambda * g_h(mu_e, sigma) * S^{PM}_{ij}

    where lambda is peak_weight and g_h is the unit
    Gaussian from `gaussian_profile`.  The result is re-normalized to
    sum to 1.

    Parameters
    ----------
    commute_morning_share, commute_evening_share:
        Required when *commute_pattern* is provided.
    """
    if commute_pattern is None:
        return {hour: base_od_share.copy(deep=True) for hour in range(nb_hours)}

    peak_weight = float(commute_pattern.peak_weight)
    if peak_weight < 0.0:
        raise ValueError("commute_pattern.peak_weight must be non-negative.")

    morning_g = gaussian_profile(nb_hours, commute_pattern.morning_peak_hour, commute_pattern.sigma_hours)
    evening_g = gaussian_profile(nb_hours, commute_pattern.evening_peak_hour, commute_pattern.sigma_hours)

    hourly_shares: Dict[int, pd.DataFrame] = {}
    for hour in range(nb_hours):
        combined = base_od_share.copy(deep=True)
        combined += peak_weight * morning_g[hour] * commute_morning_share
        combined += peak_weight * evening_g[hour] * commute_evening_share
        hourly_shares[hour] = _normalize_positive_matrix(combined)

    return hourly_shares


def generate_hourly_od_matrices(
    base_od_share: pd.DataFrame,
    hourly_totals: np.ndarray,
    interval_seconds: int = 3600,
    hourly_od_shares: Mapping[int, pd.DataFrame] | None = None,
) -> Dict[int, pd.DataFrame]:
    """Scale OD share matrices into exact integer hourly trip counts.

    Each hour's share matrix is multiplied by the corresponding hourly total
    and rounded via `allocate_integer_matrix`.  The result is keyed by
    interval begin time in seconds (0, 3600, 7200, …).
    """
    od_matrices: Dict[int, pd.DataFrame] = {}
    for hour, hourly_total in enumerate(hourly_totals):
        share = hourly_od_shares[hour] if hourly_od_shares is not None else base_od_share
        od_matrices[hour * interval_seconds] = allocate_integer_matrix(share, int(hourly_total))
    return od_matrices


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------

def parse_commute_pattern(
    raw_pattern: Mapping[str, object] | None,
    zone_ids: Sequence[str],
) -> CommutePattern | None:
    """Parse and validate an optional commute-pattern config block."""
    if not raw_pattern:
        return None

    zone_roles = _parse_zone_role_weights(raw_pattern, zone_ids)
    if zone_roles is None:
        return None

    return CommutePattern(
        zone_roles=zone_roles,
        peak_weight=float(raw_pattern.get("peak_weight", 2.5)),
        commute_distance_decay_beta=float(raw_pattern.get("commute_distance_decay_beta", 0.0)),
        morning_peak_hour=int(raw_pattern.get("morning_peak_hour", 8)),
        evening_peak_hour=int(raw_pattern.get("evening_peak_hour", 17)),
        sigma_hours=float(raw_pattern.get("sigma_hours", 2.0)),
    )


def _parse_zone_role_weights(
    raw_pattern: Mapping[str, object],
    zone_ids: Sequence[str],
) -> dict[str, ZoneRoleWeights] | None:
    """Parse zone role weights from either the structured or legacy config format.

    The structured format uses zone_role_weights mapping each zone id to
    {residential: float, work: float}.  The legacy format uses two flat
    lists residential_zones and work_zones, each zone implicitly
    receiving a weight of 1.0.
    """
    zone_set = set(zone_ids)
    raw_zone_roles = raw_pattern.get("zone_role_weights")

    if raw_zone_roles is not None:
        if not isinstance(raw_zone_roles, Mapping):
            raise ValueError("commute_pattern.zone_role_weights must be a mapping of zone ids to role weights.")

        zone_roles = {zone_id: ZoneRoleWeights() for zone_id in zone_ids}
        for raw_zone_id, raw_weights in raw_zone_roles.items():
            zone_id = str(raw_zone_id)
            if zone_id not in zone_set:
                raise ValueError(f"commute_pattern.zone_role_weights references unknown TAZ id '{zone_id}'.")
            if not isinstance(raw_weights, Mapping):
                raise ValueError(
                    f"commute_pattern.zone_role_weights['{zone_id}'] must define residential/work values."
                )
            residential = float(raw_weights.get("residential", 0.0))
            work = float(raw_weights.get("work", 0.0))
            if residential < 0.0 or work < 0.0:
                raise ValueError(
                    f"commute_pattern.zone_role_weights['{zone_id}'] values must be non-negative."
                )
            zone_roles[zone_id] = ZoneRoleWeights(residential=residential, work=work)

    else:
        # Legacy format: flat lists of residential/work zone ids (weight = 1.0)
        residential = {str(z) for z in raw_pattern.get("residential_zones", [])}
        work = {str(z) for z in raw_pattern.get("work_zones", [])}
        if not residential and not work:
            return None

        unknown = sorted((residential | work) - zone_set)
        if unknown:
            raise ValueError(f"commute_pattern references unknown TAZ ids: {unknown}")

        zone_roles = {
            zone_id: ZoneRoleWeights(
                residential=1.0 if zone_id in residential else 0.0,
                work=1.0 if zone_id in work else 0.0,
            )
            for zone_id in zone_ids
        }

    has_residential = any(role.residential > 0.0 for role in zone_roles.values())
    has_work = any(role.work > 0.0 for role in zone_roles.values())
    if not has_residential or not has_work:
        raise ValueError("commute_pattern requires at least one residential weight and one work weight.")

    return zone_roles


# ---------------------------------------------------------------------------
# External OD matrix validation
# ---------------------------------------------------------------------------

def validate_external_od_matrices(
    raw_od_matrices: Mapping[object, pd.DataFrame],
    zone_ids: list[str],
    *,
    nb_hours: int = 24,
    interval_seconds: int = 3600,
) -> Dict[int, pd.DataFrame]:
    """Validate and normalize externally provided hourly OD matrices.

    Accepts keys as either interval begin times in seconds (0, 3600, ...) or
    plain hour indices (0, 1, ..., 23).  Missing hours are filled with zero
    matrices.  All matrices are validated for non-negativity, integer values,
    zero diagonal, and matching zone ids.
    """
    expected_begins = [hour * interval_seconds for hour in range(nb_hours)]
    expected_set = set(expected_begins)
    hour_set = set(range(nb_hours))
    normalized: Dict[int, pd.DataFrame] = {}
    empty = pd.DataFrame(0, index=zone_ids, columns=zone_ids, dtype=int)

    for raw_begin, raw_matrix in raw_od_matrices.items():
        begin = _normalize_begin_key(raw_begin, expected_set, hour_set, interval_seconds)
        if begin in normalized:
            raise ValueError(f"Duplicate OD matrix interval '{raw_begin}'.")
        normalized[begin] = _validate_single_od_matrix(raw_matrix, zone_ids)

    return {begin: normalized.get(begin, empty.copy()) for begin in expected_begins}


def _normalize_begin_key(
    raw_begin: object,
    expected_set: set[int],
    hour_set: set[int],
    interval_seconds: int,
) -> int:
    try:
        numeric = int(float(raw_begin))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid OD matrix interval key '{raw_begin}'.") from exc

    if numeric in expected_set:
        return numeric
    if numeric in hour_set:
        return numeric * interval_seconds
    raise ValueError(
        f"OD matrix key '{raw_begin}' is invalid. Expected hourly begins in seconds "
        f"or hour indices 0–{len(hour_set) - 1}."
    )


def _validate_single_od_matrix(raw_matrix: pd.DataFrame, zone_ids: list[str]) -> pd.DataFrame:
    zone_set = set(zone_ids)
    unknown_rows = set(map(str, raw_matrix.index)) - zone_set
    unknown_cols = set(map(str, raw_matrix.columns)) - zone_set
    if unknown_rows or unknown_cols:
        raise ValueError(
            f"OD matrices contain unknown TAZ ids: rows={sorted(unknown_rows)} cols={sorted(unknown_cols)}"
        )

    matrix = raw_matrix.copy()
    matrix.index = matrix.index.map(str)
    matrix.columns = matrix.columns.map(str)
    matrix = matrix.reindex(index=zone_ids, columns=zone_ids, fill_value=0)

    values = matrix.to_numpy(dtype=float)
    if np.any(values < 0):
        raise ValueError("External OD matrices must not contain negative values.")
    if not np.allclose(values, np.rint(values), atol=1e-9):
        raise ValueError("External OD matrices must contain integer trip counts.")

    int_values = np.rint(values).astype(int)
    if np.any(np.diag(int_values) > 0):
        raise ValueError("External OD matrices must have zero diagonal entries.")

    return pd.DataFrame(int_values, index=zone_ids, columns=zone_ids, dtype=int)


def _normalize_positive_matrix(matrix: pd.DataFrame) -> pd.DataFrame:
    """Normalize a non-negative weighting matrix into a probability (share) matrix."""
    total = float(matrix.values.sum())
    if total <= 0.0:
        raise ValueError("OD weighting matrix has no positive mass.")
    return matrix / total
