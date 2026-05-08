"""
Network utility functions for the Monte Carlo simulation.

Handles network topology loading, simulation time-window extraction from
demand files, and vehicle rerouting counts between baseline and plan routes.

Demand generation is the responsibility of scenario_generator, which
produces a trips.xml via python -m scenario_generator.generator.
Route rewriting is handled exclusively by
deviation_plan_maker.route_rewriter.apply_detour_to_routes, which is
the single canonical implementation for the whole pipeline.
"""

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Tuple


def get_trip_time_window(trips_file: Path) -> Tuple[float, float]:
    """Return the earliest and latest departure times from a trips or routes XML file.

    Parameters
    ----------
    trips_file:
        Path to a SUMO trips.xml or routes.xml file.

    Returns
    -------
    tuple[float, float]
        (min_depart, max_depart) in seconds.  Both values are 0.0 when no
        departures are found.
    """
    root = ET.parse(str(trips_file)).getroot()
    departures: List[float] = []
    for tag in ("trip", "vehicle"):
        for elem in root.findall(tag):
            if (depart := elem.get("depart")) is not None:
                try:
                    departures.append(float(depart))
                except ValueError:
                    pass
    return (
        min(departures) if departures else 0.0,
        max(departures) if departures else 0.0,
    )


def load_edge_endpoints(net_xml: Path) -> Dict[str, Tuple[str, str]]:
    """Load edge endpoint nodes from a SUMO .net.xml file.

    Parameters
    ----------
    net_xml:
        Path to the SUMO network file.

    Returns
    -------
    dict[str, tuple[str, str]]
        Mapping edge_id -> (from_node, to_node) for every non-internal edge.
        Passed directly to deviation_plan_maker.route_rewriter.apply_detour_to_routes
        as the edge_nodes argument.
    """
    root = ET.parse(str(net_xml)).getroot()
    edge_endpoints: Dict[str, Tuple[str, str]] = {}
    for edge_elem in root.findall("edge"):
        edge_id = edge_elem.get("id")
        from_node = edge_elem.get("from")
        to_node = edge_elem.get("to")
        if not edge_id or not from_node or not to_node:
            continue
        if edge_id.startswith(":") or edge_elem.get("function") == "internal":
            continue
        edge_endpoints[edge_id] = (from_node, to_node)
    return edge_endpoints


def count_rerouted_vehicles(
    baseline_routes: Path, plan_routes: Path
) -> Tuple[int, int]:
    """Count vehicles whose edge sequence changed between baseline and plan routes.

    Compares the edges attribute of every <vehicle><route> element.
    A vehicle is counted as rerouted if its sequence differs between the two
    files (i.e. the detour was applied to it).

    Parameters
    ----------
    baseline_routes:
        Path to the original (unmodified) routes.xml file.
    plan_routes:
        Path to the plan-specific routes.xml file after detour rewriting.

    Returns
    -------
    tuple[int, int]
        (rerouted, total_vehicles) where rerouted is the number of vehicles
        whose edge sequence changed relative to the baseline.
    """
    def _edge_sequences(path: Path) -> Dict[str, str]:
        root = ET.parse(str(path)).getroot()
        sequences: Dict[str, str] = {}
        for vehicle in root.findall("vehicle"):
            route_elem = vehicle.find("route")
            sequences[vehicle.get("id", "")] = (
                route_elem.get("edges", "") if route_elem is not None else ""
            )
        return sequences

    baseline_seqs = _edge_sequences(baseline_routes)
    plan_seqs = _edge_sequences(plan_routes)

    total = len(baseline_seqs)
    rerouted = sum(
        1
        for vid, edges in baseline_seqs.items()
        if edges != plan_seqs.get(vid, edges)
    )
    return rerouted, total
