"""Candidate-route generation on the real road network.

Pipeline role: Stage 2 : Route Library.

Rather than routing every vehicle individually, the generator builds a small
library of k candidate routes for each active OD pair once, then samples
from that library during vehicle instantiation.  This design separates the
computationally expensive routing step from the stochastic sampling step and
makes the route set deterministic given a fixed seed.

The stage proceeds in three steps:

1. Impedance estimation : sample a handful of edge pairs per TAZ pair and
   take the shortest observed path cost as the TAZ-to-TAZ impedance.  This
   approximate value feeds the gravity OD model in Stage 1.
2. Candidate route computation : for each OD pair with positive demand,
   sample endpoint_samples_per_od concrete (origin-edge, destination-edge)
   pairs and compute up to k shortest simple paths for each using Yen's
   algorithm, as implemented in networkx.shortest_simple_paths.
3. Caching : the resulting library is pickled with a metadata fingerprint
   so it can be reused across runs with the same network, seed, and parameters.

Edge-expanded network
The routing graph uses SUMO edge ids as nodes (rather than junction ids).
Edges of consecutive road segments are connected with a weight equal to the
successor edge's length.  This representation produces paths that can be
used to extract the from/to endpoints written into trips.xml.
"""

from __future__ import annotations

import hashlib
import itertools
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping

import networkx as nx
import numpy as np
import pandas as pd
import sumolib

from scenario_generator.zones import TrafficZone, sample_zone_edge

CANDIDATE_ROUTE_CACHE_VERSION = 1


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RouteCandidate:
    """One feasible network route for an OD pair.

    Attributes
    ----------
    edges:
        Ordered sequence of SUMO edge ids.
    cost:
        Total length in metres (sum of edge lengths along the path).
    """

    edges: tuple[str, ...]
    cost: float


# ---------------------------------------------------------------------------
# Edge-expanded routing graph
# ---------------------------------------------------------------------------

class EdgeRouter:
    """Edge-level routing graph for private-vehicle shortest-path queries.

    Building the graph once per run and caching individual queries avoids
    redundant Dijkstra executions across the many OD pairs that share sub-paths.

    Graph construction
    ------------------
    Each drivable non-internal edge becomes a node.  A directed edge is added
    from u to v whenever road v is reachable from road u via a SUMO
    connection, with the arc weight set to the length of v.  This means path
    cost equals the sum of edge lengths along the route (metres).
    """

    def __init__(self, net: sumolib.net.Net):
        self.graph = nx.DiGraph()
        self.edge_costs: Dict[str, float] = {}
        self._k_shortest_cache: Dict[tuple[str, str, int], list[RouteCandidate]] = {}
        self._shortest_cost_cache: Dict[tuple[str, str], float | None] = {}

        # Pass 1: add all eligible edge nodes
        for edge in net.getEdges():
            edge_id = edge.getID()
            if edge_id.startswith(":") or not edge.allows("private"):
                continue
            self.graph.add_node(edge_id)
            self.edge_costs[edge_id] = max(float(edge.getLength()), 1e-9)

        # Pass 2: add directed arcs between consecutive edges
        for edge in net.getEdges():
            edge_id = edge.getID()
            if edge_id not in self.graph:
                continue
            for successor in edge.getOutgoing():
                succ_id = successor.getID()
                if succ_id not in self.graph:
                    continue
                self.graph.add_edge(edge_id, succ_id, weight=self.edge_costs[succ_id])

    def path_cost(self, path: list[str] | tuple[str, ...]) -> float:
        """Return the length-based cost (metres) of a candidate edge path."""
        return float(sum(self.edge_costs[eid] for eid in path))

    def shortest_path_cost(self, origin_edge_id: str, destination_edge_id: str) -> float | None:
        """Return the cost of the cheapest route between two edge ids, or None."""
        key = (origin_edge_id, destination_edge_id)
        if key not in self._shortest_cost_cache:
            candidates = self.k_shortest_paths(origin_edge_id, destination_edge_id, k=1)
            self._shortest_cost_cache[key] = candidates[0].cost if candidates else None
        return self._shortest_cost_cache[key]

    def k_shortest_paths(
        self, origin_edge_id: str, destination_edge_id: str, k: int
    ) -> list[RouteCandidate]:
        """Return up to k simple routes ordered by total length.

        Uses networkx.shortest_simple_paths, which internally applies
        Yen's k-shortest loopless paths algorithm.  Results are
        cached so repeated queries for the same triple are free.
        """
        key = (origin_edge_id, destination_edge_id, k)
        if key in self._k_shortest_cache:
            return self._k_shortest_cache[key]

        if origin_edge_id not in self.graph or destination_edge_id not in self.graph or k <= 0:
            return []

        try:
            path_iter = nx.shortest_simple_paths(
                self.graph,
                origin_edge_id,
                destination_edge_id,
                weight="weight",
            )
            candidates = [
                RouteCandidate(edges=tuple(path), cost=self.path_cost(path))
                for path in itertools.islice(path_iter, k)
            ]
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            candidates = []

        self._k_shortest_cache[key] = candidates
        return candidates


# ---------------------------------------------------------------------------
# Impedance estimation
# ---------------------------------------------------------------------------

def estimate_zone_impedance_matrix(
    zones: Mapping[str, TrafficZone],
    router: EdgeRouter,
    *,
    endpoint_samples_per_od: int,
    random_seed: int,
) -> pd.DataFrame:
    """Estimate TAZ-to-TAZ impedance from sampled origin/destination edge pairs.

    For each directed TAZ pair (i, j), endpoint_samples_per_od concrete
    (origin-edge, destination-edge) combinations are drawn from the two zones.
    The minimum observed shortest-path length is used as the representative
    impedance.  Pairs with no routable sample keep an impedance of inf,
    which the gravity model treats as zero weight.

    The approximation is intentionally coarse: it avoids an expensive
    all-pairs shortest-path computation while still providing a monotonically
    decreasing impedance signal for the gravity model.
    """
    zone_ids = list(zones.keys())
    impedance = pd.DataFrame(np.inf, index=zone_ids, columns=zone_ids)

    for origin in zone_ids:
        impedance.loc[origin, origin] = 0.0
        for destination in zone_ids:
            if origin == destination:
                continue

            pair_rng = make_pair_rng(random_seed, origin, destination)
            best_cost: float | None = None
            for _ in range(endpoint_samples_per_od):
                o_edge = sample_zone_edge(zones[origin], pair_rng)
                d_edge = sample_zone_edge(zones[destination], pair_rng)
                cost = router.shortest_path_cost(o_edge, d_edge)
                if cost is not None and (best_cost is None or cost < best_cost):
                    best_cost = cost

            if best_cost is not None:
                impedance.loc[origin, destination] = best_cost

    return impedance


# ---------------------------------------------------------------------------
# Candidate route library
# ---------------------------------------------------------------------------

def build_candidate_route_library(
    hourly_od_matrices: Mapping[int, pd.DataFrame],
    zones: Mapping[str, TrafficZone],
    router: EdgeRouter,
    *,
    k_paths: int,
    endpoint_samples_per_od: int,
    random_seed: int,
    cache_path: str | Path,
) -> tuple[dict[tuple[str, str], list[RouteCandidate]], list[tuple[str, str]]]:
    """Build or load the candidate route library for all active OD pairs.

    For each OD pair with positive demand in at least one hour, the function
    samples endpoint_samples_per_od (origin-edge, destination-edge) pairs
    and computes up to k_paths shortest simple paths per pair using
    EdgeRouter.k_shortest_paths.  Duplicate routes across samples are
    deduplicated; if the same edge sequence appears more than once, only the
    lowest-cost instance is kept.  The final library retains at most
    ``k_paths`` candidates per OD pair, sorted by cost.

    Returns
    -------
    route_library:
        Mapping from (origin_zone_id, destination_zone_id) to a sorted list
        of RouteCandidate objects.
    unroutable_pairs:
        OD pairs for which no routable path could be found.
    """
    active_pairs = sorted(extract_active_pairs(hourly_od_matrices))
    cache_file = Path(cache_path)

    cached = _load_cached_route_library(
        cache_file,
        active_pairs=active_pairs,
        k_paths=k_paths,
        endpoint_samples_per_od=endpoint_samples_per_od,
        random_seed=random_seed,
    )
    if cached is not None:
        return cached

    route_library: dict[tuple[str, str], list[RouteCandidate]] = {}
    unroutable_pairs: list[tuple[str, str]] = []

    for origin, destination in active_pairs:
        pair_rng = make_pair_rng(random_seed, origin, destination)
        seen: dict[tuple[str, ...], RouteCandidate] = {}

        for _ in range(endpoint_samples_per_od):
            o_edge = sample_zone_edge(zones[origin], pair_rng)
            d_edge = sample_zone_edge(zones[destination], pair_rng)
            for candidate in router.k_shortest_paths(o_edge, d_edge, k_paths):
                existing = seen.get(candidate.edges)
                if existing is None or candidate.cost < existing.cost:
                    seen[candidate.edges] = candidate

        candidates = sorted(seen.values(), key=lambda c: (c.cost, c.edges))[:k_paths]

        if not candidates:
            unroutable_pairs.append((origin, destination))
        else:
            route_library[(origin, destination)] = candidates

    _store_route_library_cache(
        cache_file,
        route_library,
        unroutable_pairs,
        active_pairs=active_pairs,
        k_paths=k_paths,
        endpoint_samples_per_od=endpoint_samples_per_od,
        random_seed=random_seed,
    )
    return route_library, unroutable_pairs


def extract_active_pairs(hourly_od_matrices: Mapping[int, pd.DataFrame]) -> set[tuple[str, str]]:
    """Collect all OD pairs with strictly positive demand in at least one hour."""
    active_pairs: set[tuple[str, str]] = set()
    for matrix in hourly_od_matrices.values():
        for origin in matrix.index:
            for destination in matrix.columns:
                if int(matrix.loc[origin, destination]) > 0:
                    active_pairs.add((str(origin), str(destination)))
    return active_pairs


def make_pair_rng(random_seed: int, origin: str, destination: str) -> np.random.Generator:
    """Create a stable, deterministic RNG stream for one OD pair.

    Using a hash of the global seed and the zone ids ensures that each pair
    gets an independent, reproducible stream regardless of iteration order.
    """
    digest = hashlib.sha256(f"{random_seed}:{origin}->{destination}".encode()).digest()
    pair_seed = int.from_bytes(digest[:8], "big", signed=False)
    return np.random.default_rng(pair_seed)


# ---------------------------------------------------------------------------
# Route library cache (pickle + metadata fingerprint)
# ---------------------------------------------------------------------------

def _load_cached_route_library(
    cache_file: Path,
    *,
    active_pairs: list[tuple[str, str]],
    k_paths: int,
    endpoint_samples_per_od: int,
    random_seed: int,
) -> tuple[dict[tuple[str, str], list[RouteCandidate]], list[tuple[str, str]]] | None:
    """Return the cached library if the metadata fingerprint matches, else None."""
    if not cache_file.exists():
        return None

    with open(cache_file, "rb") as fp:
        payload = pickle.load(fp)

    expected_meta = {
        "version": CANDIDATE_ROUTE_CACHE_VERSION,
        "active_pairs": active_pairs,
        "k_paths": k_paths,
        "endpoint_samples_per_od": endpoint_samples_per_od,
        "random_seed": random_seed,
    }
    if payload.get("metadata") != expected_meta:
        return None

    route_library = {
        tuple(pair): [
            RouteCandidate(tuple(c["edges"]), float(c["cost"]))
            for c in candidates
        ]
        for pair, candidates in payload.get("routes", {}).items()
    }
    unroutable = [tuple(pair) for pair in payload.get("unroutable_pairs", [])]
    return route_library, unroutable


def _store_route_library_cache(
    cache_file: Path,
    route_library: Mapping[tuple[str, str], list[RouteCandidate]],
    unroutable_pairs: list[tuple[str, str]],
    *,
    active_pairs: list[tuple[str, str]],
    k_paths: int,
    endpoint_samples_per_od: int,
    random_seed: int,
) -> None:
    """Persist the route library with a metadata fingerprint for cache validation."""
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "metadata": {
            "version": CANDIDATE_ROUTE_CACHE_VERSION,
            "active_pairs": active_pairs,
            "k_paths": k_paths,
            "endpoint_samples_per_od": endpoint_samples_per_od,
            "random_seed": random_seed,
        },
        "unroutable_pairs": [list(pair) for pair in unroutable_pairs],
        "routes": {
            pair: [{"edges": list(c.edges), "cost": c.cost} for c in candidates]
            for pair, candidates in route_library.items()
        },
    }
    with open(cache_file, "wb") as fp:
        pickle.dump(payload, fp)
