"""Deviation plan computation and route rewriting for road closures.

The package is organised around three modules:

1. deviation_plan_maker.network_graph — builds a directed graph from a
   SUMO net.xml file and computes detour plans using Dijkstra and Yen's
   k-shortest-paths algorithms.
2. deviation_plan_maker.deviation_plan — data classes shared between
   the graph module and the rewriter.
3. deviation_plan_maker.route_rewriter — splices detour plans into
   SUMO routes.xml files using depth-aware plan selection.
"""

from deviation_plan_maker.deviation_plan import DeviationPlan, PathResult
from deviation_plan_maker.network_graph import NetworkGraph, EdgeRecord
from deviation_plan_maker.route_rewriter import apply_detour_to_routes

__all__ = [
    "DeviationPlan",
    "PathResult",
    "NetworkGraph",
    "EdgeRecord",
    "apply_detour_to_routes",
]
