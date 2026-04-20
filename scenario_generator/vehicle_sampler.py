"""Vehicle instantiation from hourly OD demand and candidate routes.

Pipeline role: **Stage 3 — Vehicle Sampling**.

Given the hourly OD matrices from Stage 1 and the candidate route library
from Stage 2, this module produces one concrete ``(departure_time, edge_list)``
tuple per trip.

Departure time
--------------
Trips within each one-hour interval are assumed to depart uniformly over
[begin, begin + interval_seconds].  This is the standard assumption when
only hourly demand totals are available.

Route choice
------------
One route is selected uniformly at random from the pre-computed candidate set
for each OD pair.  All k candidates are treated as equally likely, which
is the maximum-entropy (least informative) assumption given no additional
preference data.
"""

from __future__ import annotations

from typing import Mapping

import numpy as np
import pandas as pd

from scenario_generator.route_library import RouteCandidate


def sample_vehicles_from_od(
    hourly_od_matrices: Mapping[int, pd.DataFrame],
    route_library: Mapping[tuple[str, str], list[RouteCandidate]],
    *,
    interval_seconds: int,
    rng: np.random.Generator,
) -> list[tuple[float, list[str]]]:
    """Instantiate one concrete route per trip implied by the OD matrices.

    For each positive OD cell the function draws departure times uniformly
    over the interval and picks a route uniformly at random from the
    candidate set for that pair.

    Parameters
    ----------
    hourly_od_matrices:
        Keyed by interval begin time (seconds).  Each value is a square
        integer DataFrame with TAZ ids as both index and columns.
    route_library:
        Candidate routes per OD pair, as produced by Stage 2.
    interval_seconds:
        Duration of each OD interval (must match the matrix keys).
    rng:
        Numpy random generator for all stochastic draws.

    Returns
    -------
    list of (departure_time, edge_list) tuples, sorted by departure time.
    """
    vehicles: list[tuple[float, list[str]]] = []

    for begin in sorted(hourly_od_matrices):
        matrix = hourly_od_matrices[begin]
        end = begin + interval_seconds

        for origin in matrix.index:
            for destination in matrix.columns:
                trip_count = int(matrix.loc[origin, destination])
                if trip_count <= 0:
                    continue

                od_pair = (str(origin), str(destination))
                candidates = route_library.get(od_pair)
                if not candidates:
                    raise ValueError(f"OD pair {od_pair} has demand but no candidate routes.")

                # Departure times: uniform over the interval
                departures = np.sort(rng.uniform(begin, end, size=trip_count))

                # Route choice: uniform random selection from the candidate set
                choices = rng.integers(0, len(candidates), size=trip_count)

                for depart, idx in zip(departures, choices, strict=True):
                    vehicles.append((float(depart), list(candidates[int(idx)].edges)))

    vehicles.sort(key=lambda v: v[0])
    return vehicles
