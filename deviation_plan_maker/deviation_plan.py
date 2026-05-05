"""Data classes for deviation plans and path results.

These two classes are the shared data contract between the graph module
(network_graph) and the route-rewriting module (route_rewriter).
Neither module depends on the other; they communicate exclusively through
these structures.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PathResult:
    """Compact representation of a detour path through the network.

    Attributes
    ----------
    nodes:
        Ordered list of junction ids traversed by the path.
    edges:
        Ordered list of edge ids (one per hop between consecutive nodes).
    cumulative_costs:
        Routing cost accumulated at each node, starting with 0.0 for
        the origin.  Units match the edge weights supplied to the graph
        (free-flow travel time in seconds by default).
    """

    nodes: list[str]
    edges: list[str]
    cumulative_costs: list[float]

    @property
    def total_cost(self) -> float:
        """Total routing cost of the path (last element of cumulative_costs).

        Units match the edge weights used during graph construction (seconds
        for free-flow travel time, metres for length-based routing).
        Returns 0.0 for an empty path.
        """
        return self.cumulative_costs[-1] if self.cumulative_costs else 0.0


@dataclass
class DeviationPlan:
    """A detour proposal produced by NetworkGraph.

    Attributes
    ----------
    plan_id:
        Internal identifier string (e.g. "plan_1").
    rank:
        Rank within the candidate set (1 = best by cost).
    depth:
        BFS depth of source_node relative to the closure's immediate
        upstream junction.  depth=0 means the plan starts exactly at
        that junction and acts as the universal fallback; higher values
        indicate plans that originate further upstream and can absorb
        vehicles approaching from those directions.
    path:
        PathResult holding nodes, edges, and cumulative routing costs.
    total_length_m:
        Sum of geometric edge lengths along the detour in metres.  This is
        distinct from path.total_cost, which is in routing-weight units
        (e.g. seconds).
    source_node:
        First junction of the detour path.
    target_node:
        Last junction of the detour path.
    notes:
        Optional diagnostic string produced during endpoint computation
        (e.g. when the source or target junction was adjusted upstream or
        downstream to find a suitable branching point).
    """

    plan_id: str
    rank: int
    depth: int
    path: PathResult
    total_length_m: float
    source_node: str | None
    target_node: str | None
    notes: str | None = None

    @property
    def detour_edges(self) -> list[str]:
        """Ordered list of edge ids composing the detour path."""
        return self.path.edges
