"""
Route rewriting utilities for applying deviation plans to SUMO route files.

For each vehicle route that crosses a closed edge, the rewriter selects the
deepest deviation plan whose source node appears in that vehicle's route
before the closed segment, then splices the detour in starting from that
node.  Shallower plans are tried in turn until the depth-0 plan is reached,
which originates at the closure's immediate source junction and is therefore
guaranteed to apply to every affected vehicle.
"""

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from deviation_plan_maker.deviation_plan import DeviationPlan


def apply_detour_to_routes(
    input_routes: Path,
    output_routes: Path,
    plans: Sequence[DeviationPlan],
    closed_edges: Iterable[str],
    edge_nodes: Dict[str, Tuple[str, str]],
) -> None:
    """Rewrite a SUMO routes.xml using depth-aware deviation plan selection.

    For every <vehicle>, <trip>, and <flow> element whose edge
    sequence contains at least one closed edge, this function:

    1. Collects all junctions visited by the vehicle before the closed
       segment (both endpoints of every preceding edge).
    2. Iterates through plans from deepest to shallowest and picks the
       first plan whose source_node is among those junctions.
    3. Walks backward in the route from the closed segment to find the edge
       that departs from source_node, then replaces every edge from that
       position through the end of the closed segment with the plan's detour
       edge sequence.

    The depth-0 plan (source_node = the closure's immediate upstream
    junction) acts as the universal fallback because every vehicle that
    reaches the closed edge must pass through that junction first.

    Parameters
    ----------
    input_routes:
        Path to the input routes.xml file.
    output_routes:
        Path to write the rewritten routes.xml file.
    plans:
        Candidate deviation plans sorted depth descending, as returned by
        NetworkGraph.compute_upstream_deviation_plans.
    closed_edges:
        Iterable of edge ids forming the closed segment.
    edge_nodes:
        Mapping edge_id -> (from_node, to_node), obtained from
        NetworkGraph.get_edge_nodes.

    Raises
    ------
    FileNotFoundError
        If input_routes does not exist.
    ValueError
        If no depth-0 plan is present in plans.
    """
    if not input_routes.exists():
        raise FileNotFoundError(f"Routes file not found at {input_routes}")

    closed_set = set(closed_edges)

    if not plans or not closed_set:
        output_routes.write_text(
            input_routes.read_text(encoding="utf-8"), encoding="utf-8"
        )
        return

    if not any(p.depth == 0 for p in plans):
        raise ValueError(
            "plans must contain at least one depth-0 plan to guarantee "
            "coverage for all affected vehicles."
        )

    # Plans are expected depth-descending; enforce it defensively.
    sorted_plans = sorted(plans, key=lambda p: (-p.depth, p.total_length_m))

    tree, root = _parse_routes_xml(input_routes)

    for vehicle in root.findall("vehicle"):
        route_elem = vehicle.find("route")
        if route_elem is not None:
            _rewrite_attr(route_elem, "edges", sorted_plans, closed_set, edge_nodes)

    for trip in root.findall("trip"):
        _rewrite_attr(trip, "edges", sorted_plans, closed_set, edge_nodes)

    for flow in root.findall("flow"):
        route_elem = flow.find("route")
        if route_elem is not None:
            _rewrite_attr(route_elem, "edges", sorted_plans, closed_set, edge_nodes)
        else:
            _rewrite_attr(flow, "edges", sorted_plans, closed_set, edge_nodes)

    tree.write(str(output_routes), encoding="utf-8", xml_declaration=True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_routes_xml(routes_path: Path) -> Tuple[ET.ElementTree, ET.Element]:
    """Parse routes XML and return (tree, root)."""
    tree = ET.parse(str(routes_path))
    return tree, tree.getroot()


def _rewrite_attr(
    elem: ET.Element,
    attr: str,
    plans: Sequence[DeviationPlan],
    closed_set: Set[str],
    edge_nodes: Dict[str, Tuple[str, str]],
) -> None:
    """Rewrite the edge-sequence attribute of a single XML element in place."""
    raw = elem.get(attr, "")
    if not raw:
        return
    edge_list = raw.split()
    plan = _pick_plan(edge_list, closed_set, plans, edge_nodes)
    if plan is None:
        return  # route not affected by the closure
    new_edges = _rewrite_with_plan(edge_list, plan, closed_set, edge_nodes)
    elem.set(attr, " ".join(new_edges))


def _pick_plan(
    edges: List[str],
    closed_set: Set[str],
    plans: Sequence[DeviationPlan],
    edge_nodes: Dict[str, Tuple[str, str]],
) -> Optional[DeviationPlan]:
    """Return the deepest applicable plan for this vehicle route.

    A plan is applicable when its source_node is among the junctions the
    vehicle visits before the closed segment.  Plans are tried in
    depth-descending order; the first match is returned.

    Returns None if the route does not cross any closed edge at all.
    """
    closed_idx = next(
        (i for i, e in enumerate(edges) if e in closed_set), None
    )
    if closed_idx is None:
        return None  # this vehicle is unaffected

    # Gather every junction visited before the closed segment.
    # Both endpoints of each preceding edge are included so that the
    # junction the vehicle *arrives at* (to_node) is also captured.
    nodes_before: Set[str] = set()
    for e in edges[:closed_idx]:
        pair = edge_nodes.get(e)
        if pair:
            nodes_before.add(pair[0])  # from_node
            nodes_before.add(pair[1])  # to_node

    # Always add the from_node of the first closed edge.  When the route
    # starts with the closed edge (closed_idx == 0) nodes_before would
    # otherwise be empty, causing even the depth-0 fallback — whose
    # source_node equals that junction — to be skipped.
    first_closed_from = edge_nodes.get(edges[closed_idx], (None, None))[0]
    if first_closed_from:
        nodes_before.add(first_closed_from)

    for plan in plans:  # deepest first
        if plan.source_node in nodes_before:
            return plan

    return None  # unreachable when a valid depth-0 plan exists


def _rewrite_with_plan(
    edges: List[str],
    plan: DeviationPlan,
    closed_set: Set[str],
    edge_nodes: Dict[str, Tuple[str, str]],
) -> List[str]:
    """Splice the detour into the edge sequence.

    Replaces the contiguous closed segment AND every preceding edge that
    forms the approach from plan.source_node onward with the plan's
    detour edges.

    For depth-0 plans, source_node is the from_node of the closed edge
    itself, so no preceding edges are replaced — only the closed segment is
    swapped out, which is the correct behaviour.

    For deeper plans, the function walks backward from the first closed edge
    to find the edge in the vehicle's route that departs from
    source_node (i.e., whose from_node equals source_node), and
    uses that as the replacement start so the splice is topology-valid.
    """
    closed_idx = next(i for i, e in enumerate(edges) if e in closed_set)

    # Walk backward to find the edge departing from source_node.
    replace_start = closed_idx  # default for depth-0 (no approach to replace)
    for i in range(closed_idx - 1, -1, -1):
        pair = edge_nodes.get(edges[i])
        if pair and pair[0] == plan.source_node:
            replace_start = i
            break

    # Advance past all consecutive closed edges.
    replace_end = closed_idx
    while replace_end < len(edges) and edges[replace_end] in closed_set:
        replace_end += 1

    # If endpoint search adjusted the detour target downstream, discard the
    # original route segment until the first edge departing from that target
    # node.  Keeping the old suffix from the closure's immediate target would
    # create a discontinuous SUMO route.
    if plan.target_node:
        while replace_end < len(edges):
            pair = edge_nodes.get(edges[replace_end])
            if pair and pair[0] == plan.target_node:
                break
            replace_end += 1

    return edges[:replace_start] + list(plan.detour_edges) + edges[replace_end:]
