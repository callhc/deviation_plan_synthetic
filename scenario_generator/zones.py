"""TAZ parsing and zone-level edge sampling.

Pipeline role: **Stage 1 support — Network Zoning**.

This module turns a SUMO TAZ file into a compact in-memory representation
used by both the OD model (zone masses) and the route library (edge sampling).
Each :class:`TrafficZone` stores only what the pipeline needs:

- a unique TAZ identifier,
- the drivable edges belonging to that zone,
- per-edge weights used for both zone mass and probabilistic endpoint sampling.

Edge weight proxy
-----------------
Zone mass is approximated as the sum of (length × lane count) over all
private-vehicle edges in the TAZ.  This gives larger, higher-capacity roads
more influence on both OD generation and route-endpoint sampling, which is a
reasonable heuristic when detailed land-use data are unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

import numpy as np
import sumolib


def _edge_weight(edge: sumolib.net.edge.Edge) -> float:
    """Return the capacity-weighted length of one edge.

    Using ``length × lanes`` as a proxy for road capacity makes arterials and
    motorway links contribute more to zone mass than minor side streets, which
    aligns with their empirical trip-generation role.
    """
    return max(float(edge.getLength()), 1.0) * max(int(edge.getLaneNumber()), 1)


@dataclass(frozen=True)
class TrafficZone:
    """Traffic Analysis Zone with drivable edges and sampling weights.

    Attributes
    ----------
    zone_id:
        Unique string identifier matching the TAZ file.
    edge_ids:
        Tuple of SUMO edge ids that can carry private-vehicle traffic.
    edge_weights:
        Per-edge capacity-weighted lengths (same order as *edge_ids*).
    mass:
        Scalar zone mass = sum of all edge weights; used by the gravity model.
    """

    zone_id: str
    edge_ids: tuple[str, ...]
    edge_weights: tuple[float, ...]
    mass: float

    @property
    def sampling_probabilities(self) -> np.ndarray:
        """Normalized probability vector for endpoint-edge sampling."""
        weights = np.asarray(self.edge_weights, dtype=float)
        return weights / weights.sum()


def load_taz_zones(net: sumolib.net.Net, taz_file: str) -> Dict[str, TrafficZone]:
    """Load TAZs from disk and discard edges that cannot carry private traffic.

    Edges are filtered to those that allow the ``private`` vehicle class.
    Internal SUMO junction edges (ids starting with ``:``) are always excluded.

    Raises
    ------
    ValueError
        If a TAZ has no valid edges, or if the file contains no TAZs at all.
    """
    zones: Dict[str, TrafficZone] = {}

    for taz in sumolib.xml.parse_fast(taz_file, "taz", ["id", "edges"]):
        valid_edge_ids: list[str] = []
        edge_weights: list[float] = []

        for edge_id in taz.edges.split():
            try:
                edge = net.getEdge(edge_id)
            except KeyError:
                # Edge listed in the TAZ file is absent from the network: skip.
                continue
            if edge is None or not edge.allows("private"):
                continue
            valid_edge_ids.append(edge_id)
            edge_weights.append(_edge_weight(edge))

        if not valid_edge_ids:
            raise ValueError(f"TAZ '{taz.id}' has no valid private-vehicle edges.")

        zones[taz.id] = TrafficZone(
            zone_id=taz.id,
            edge_ids=tuple(valid_edge_ids),
            edge_weights=tuple(edge_weights),
            mass=float(sum(edge_weights)),
        )

    if not zones:
        raise ValueError(f"No TAZ zones could be loaded from '{taz_file}'.")

    return dict(sorted(zones.items()))


def sample_zone_edge(zone: TrafficZone, rng: np.random.Generator) -> str:
    """Sample one drivable edge from a TAZ, weighted by capacity-length."""
    idx = int(rng.choice(len(zone.edge_ids), p=zone.sampling_probabilities))
    return zone.edge_ids[idx]
