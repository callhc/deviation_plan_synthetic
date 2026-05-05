"""Lightweight directed graph for SUMO network detour planning.

Converts a SUMO net.xml file into a node-level directed graph and exposes
two complementary path-finding APIs:

- NetworkGraph.compute_deviation_plans — Yen's k-shortest paths from
  a fixed closure source junction (depth-0 plans only).
- NetworkGraph.compute_upstream_deviation_plans — reverse BFS to
  discover upstream origins, then Dijkstra from each origin (multi-depth plans).

All graph operations are implemented from scratch without external libraries.
Edge weights default to geometric length in meters, custom weights (e.g.
free-flow travel time) can be loaded from a SUMO edgedata file.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from deviation_plan_maker.deviation_plan import DeviationPlan, PathResult

logger = logging.getLogger(__name__)


@dataclass
class EdgeRecord:
    """Minimal description of a SUMO edge needed for detour planning.

    Attributes
    ----------
    edge_id:
        SUMO edge identifier.
    from_node:
        Upstream junction id.
    to_node:
        Downstream junction id.
    length_m:
        Geometric edge length in metres.
    """

    edge_id: str
    from_node: str
    to_node: str
    length_m: float


def get_edge_length(edge_elem: ET.Element) -> float:
    """Extract the best length estimate available for a SUMO edge element.

    Tries the length attribute on the edge first, then falls back to
    the length attribute on the first <lane> child.  Returns 0.0
    and logs a warning when no length is found.
    """
    edge_length = edge_elem.get("length")
    if edge_length is not None:
        try:
            return float(edge_length.strip())
        except ValueError:
            pass
    for lane in edge_elem.findall("lane"):
        lane_len = lane.get("length")
        if lane_len is not None:
            try:
                return float(lane_len.strip())
            except ValueError:
                continue
    logger.warning("Edge %s has no length defined; defaulting to 0.0", edge_elem.get("id"))
    return 0.0


class NetworkGraph:
    """Lightweight directed graph representation of a SUMO network.

    Exposes helper methods to compute shortest detours while enforcing
    edge and node exclusions.  All graph operations are implemented from
    scratch — no external graph library dependency.
    """

    def __init__(
        self,
        edges: dict[str, EdgeRecord],
        outgoing: dict[str, list[EdgeRecord]],
        incoming: dict[str, list[EdgeRecord]],
    ):
        self.edges = edges
        self.outgoing = outgoing
        self.incoming = incoming
        self.edge_weights: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_net_xml(cls, net_xml_path: Path) -> NetworkGraph:
        """Load the network graph from a SUMO net.xml file.

        Parameters
        ----------
        net_xml_path:
            Path to the .net.xml file.

        Returns
        -------
        NetworkGraph
            Populated graph with all non-internal edges.
        """
        tree = ET.parse(str(net_xml_path))
        root = tree.getroot()
        edges: dict[str, EdgeRecord] = {}
        outgoing: dict[str, list[EdgeRecord]] = {}
        incoming: dict[str, list[EdgeRecord]] = {}
        for edge_elem in root.findall("edge"):
            edge_id = edge_elem.get("id")
            if not edge_id or edge_id.startswith(":"):
                continue
            if edge_elem.get("function") == "internal":
                continue
            from_node = edge_elem.get("from")
            to_node = edge_elem.get("to")
            if not from_node or not to_node:
                continue
            length = get_edge_length(edge_elem)
            record = EdgeRecord(
                edge_id=edge_id,
                from_node=from_node,
                to_node=to_node,
                length_m=length,
            )
            edges[edge_id] = record
            outgoing.setdefault(from_node, []).append(record)
            incoming.setdefault(to_node, []).append(record)
        return cls(edges=edges, outgoing=outgoing, incoming=incoming)

    # ------------------------------------------------------------------
    # Edge weights
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_edge_values(
        root: ET.Element,
        field: str,
        *,
        invert: bool = False,
    ) -> dict[str, float]:
        """Parse and aggregate edge values from an edgedata XML root element.

        Values are averaged across all intervals.  When *invert* is True
        the reciprocal of the mean is stored (useful for converting speed
        to travel-time weights).  Edges with non-positive values are skipped.

        Parameters
        ----------
        root:
            Root element of a parsed edgedata XML document.
        field:
            Attribute name to read from each <edge> element.
        invert:
            When True, store 1 / mean instead of mean.

        Returns
        -------
        dict[str, float]
            Mapping from edge id to aggregated (possibly inverted) value.
        """
        edge_values: dict[str, list[float]] = {}
        for interval in root.findall("interval"):
            for edge_elem in interval.findall("edge"):
                edge_id = edge_elem.get("id")
                if not edge_id:
                    continue
                value_str = edge_elem.get(field)
                if value_str is None:
                    continue
                try:
                    value = float(value_str)
                    if value <= 0:
                        continue
                    edge_values.setdefault(edge_id, []).append(value)
                except ValueError:
                    continue
        result: dict[str, float] = {}
        for edge_id, values in edge_values.items():
            mean_val = sum(values) / len(values)
            result[edge_id] = (1.0 / mean_val) if invert else mean_val
        return result

    def load_edge_weights_from_edgedata(
        self,
        edgedata_xml: Path,
        field: str,
        *,
        invert: bool = False,
        default_value: float = 1.0,
    ) -> None:
        """Load edge weights from a SUMO edgedata.xml file.

        Weights are aggregated across all intervals by taking the mean.
        Edges absent from the file keep no custom weight and fall back to
        their geometric length during routing.

        Parameters
        ----------
        edgedata_xml:
            Path to the edgedata file.
        field:
            XML attribute to read (e.g. "speed" or "traveltime").
        invert:
            When True, store the reciprocal (e.g. to convert speed
            to travel-time cost).
        default_value:
            Unused placeholder kept for API compatibility.
        """
        if not edgedata_xml.exists():
            logger.warning("Edgedata file not found: %s", edgedata_xml)
            return
        root = ET.parse(str(edgedata_xml)).getroot()
        parsed = self._parse_edge_values(root, field, invert=invert)
        for edge_id, weight in parsed.items():
            if edge_id in self.edges:
                self.edge_weights[edge_id] = weight
        logger.info(
            "Loaded %d edge weights from field '%s' (invert=%s)",
            len(self.edge_weights), field, invert,
        )

    def clear_edge_weights(self) -> None:
        """Clear all custom edge weights, reverting to length-based routing."""
        self.edge_weights.clear()
        logger.info("Cleared all custom edge weights")

    def load_edge_weights_from_xml_string(
        self,
        xml_content: str,
        field: str,
        *,
        invert: bool = False,
    ) -> None:
        """Load edge weights from an edgedata XML string.

        Parameters
        ----------
        xml_content:
            Raw XML string (same schema as an edgedata file).
        field:
            Attribute name to read from each <edge> element.
        invert:
            When True, store the reciprocal of the mean value.
        """
        root = ET.fromstring(xml_content)
        parsed = self._parse_edge_values(root, field, invert=invert)
        for edge_id, weight in parsed.items():
            if edge_id in self.edges:
                self.edge_weights[edge_id] = weight
        logger.info(
            "Loaded %d edge weights from field '%s' (invert=%s, source=string)",
            len(self.edge_weights), field, invert,
        )

    # ------------------------------------------------------------------
    # Dijkstra's algorithm
    # ------------------------------------------------------------------

    def _dijkstra(
        self,
        source: str,
        target: str,
        *,
        blocked_edges: set[str],
    ) -> PathResult | None:
        """Shortest-path search using Dijkstra's algorithm with blocked edges.

        Parameters
        ----------
        source:
            Origin junction id.
        target:
            Destination junction id.
        blocked_edges:
            Edge ids that must not be used.

        Returns
        -------
        PathResult or None
            The shortest path, or None when no path exists.
        """
        logger.debug("Dijkstra: %s → %s (blocked=%d edges)", source, target, len(blocked_edges))
        dist, prev_node, prev_edge, pq = self._init_dijkstra_state(source)
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, float("inf")):
                continue
            if u == target:
                break
            self._relax_neighbors(u, d, blocked_edges, dist, prev_node, prev_edge, pq)
        if target not in dist:
            logger.debug("Dijkstra: no path found %s → %s", source, target)
            return None
        nodes, edges, cumulative = self._reconstruct_path(
            source, target, dist, prev_node, prev_edge
        )
        logger.debug("Dijkstra: path found %s → %s (%d hops)", source, target, len(edges))
        return PathResult(nodes=nodes, edges=edges, cumulative_costs=cumulative)

    def _init_dijkstra_state(
        self, source: str
    ) -> tuple[dict[str, float], dict[str, str], dict[str, str], list[tuple[float, str]]]:
        """Initialise Dijkstra's algorithm state for *source*."""
        dist: dict[str, float] = {source: 0.0}
        prev_node: dict[str, str] = {}
        prev_edge: dict[str, str] = {}
        pq: list[tuple[float, str]] = [(0.0, source)]
        return dist, prev_node, prev_edge, pq

    def _relax_neighbors(
        self,
        u: str,
        d: float,
        blocked_edges: set[str],
        dist: dict[str, float],
        prev_node: dict[str, str],
        prev_edge: dict[str, str],
        pq: list[tuple[float, str]],
    ) -> None:
        """Relax all outgoing neighbours of *u* and update the priority queue."""
        for rec in self.outgoing.get(u, []):
            if rec.edge_id in blocked_edges:
                continue
            v = rec.to_node
            step = (
                self.edge_weights[rec.edge_id]
                if rec.edge_id in self.edge_weights
                else max(rec.length_m, 1e-9)
            )
            nd = d + step
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev_node[v] = u
                prev_edge[v] = rec.edge_id
                heapq.heappush(pq, (nd, v))

    def _reconstruct_path(
        self,
        source: str,
        target: str,
        dist: dict[str, float],
        prev_node: dict[str, str],
        prev_edge: dict[str, str],
    ) -> tuple[list[str], list[str], list[float]]:
        """Reconstruct the shortest path from Dijkstra's predecessor maps."""
        nodes_rev, edges_rev = [], []
        cur = target
        while cur != source:
            nodes_rev.append(cur)
            edges_rev.append(prev_edge[cur])
            cur = prev_node[cur]
        nodes_rev.append(source)
        nodes = list(reversed(nodes_rev))
        edges = list(reversed(edges_rev))
        cumulative = [dist[n] for n in nodes]
        return nodes, edges, cumulative

    # ------------------------------------------------------------------
    # Public shortest-path API
    # ------------------------------------------------------------------

    def shortest_path(
        self,
        source: str,
        target: str,
        *,
        blocked_edges: set[str] | None = None,
    ) -> PathResult | None:
        """Compute the shortest path from *source* to *target*.

        Parameters
        ----------
        source:
            Origin junction id.
        target:
            Destination junction id.
        blocked_edges:
            Optional set of edge ids to exclude from the search.

        Returns
        -------
        PathResult or None
            The shortest path, or None when unreachable.
        """
        return self._dijkstra(source, target, blocked_edges=blocked_edges or set())

    # ------------------------------------------------------------------
    # Upstream BFS and deviation plan generation
    # ------------------------------------------------------------------

    def _get_upstream_nodes(
        self, source: str, depth: int, excluded: set[str] | None = None
    ) -> dict[str, int]:
        """Return all nodes reachable upstream from *source* mapped to their BFS depth.

        The source node itself is at depth 0.  Nodes discovered one hop
        upstream are at depth 1, two hops at depth 2, and so on up to *depth*.

        *excluded* prevents the BFS frontier from entering certain nodes.
        Pass {t} (the closure's target junction) to stop the backward
        walk from crossing the closure boundary through a cycle.  Without
        this guard, a cycle such as t→207→…→s→[closed]→t would allow the
        BFS to discover t as a predecessor of a cycle node, and t's own
        predecessors — which are post-closure nodes — would then become
        invalid detour sources.

        Parameters
        ----------
        source:
            Starting node for the reverse BFS.
        depth:
            Maximum number of upstream hops.
        excluded:
            Nodes that must never enter the frontier.

        Returns
        -------
        dict[str, int]
            Mapping from node id to its BFS depth (0 = *source*).
        """
        _excl = excluded or set()
        node_depth: dict[str, int] = {source: 0}
        frontier: set[str] = {source}
        for d in range(1, depth + 1):
            next_frontier: set[str] = set()
            for node in frontier:
                for rec in self.incoming.get(node, []):
                    fn = rec.from_node
                    if fn not in node_depth and fn not in _excl:
                        node_depth[fn] = d
                        next_frontier.add(fn)
            frontier = next_frontier
        return node_depth

    def compute_upstream_deviation_plans(
        self,
        closed_edges: list[str] | tuple[str, ...],
        *,
        depth: int = 1,
    ) -> tuple[list[DeviationPlan], list[str], set[str]]:
        """Generate depth-tagged detour plans from upstream origins of the closure.

        Performs a reverse BFS from the closure's source junction s up to
        depth hops (including s itself at depth 0).  For every
        discovered node a Dijkstra query is run toward the closure's target
        junction t with the full blocked set.  Duplicate edge sequences
        are collapsed by a signature set.

        The returned plans are sorted by depth descending, then by total
        routing cost ascending within the same depth.  This ordering lets the
        route rewriter try the most spatially specific (deepest) plan first and
        fall back to shallower ones until the universal depth-0 plan is reached.
        The depth-0 plan (origin = s) is always present when any path from
        s to t exists and serves as the guaranteed fallback for every
        vehicle affected by the closure.

        Parameters
        ----------
        closed_edges:
            Edge ids to close/block.
        depth:
            Maximum number of hops upstream to explore.

        Returns
        -------
        deviation_plans : list[DeviationPlan]
            Plans sorted depth descending, then routing cost ascending.
        missing_edges : list[str]
            Closed edges not found in the graph.
        blocked_edges : set[str]
            Full set of edges excluded from detour routing.
        """
        existing, missing = self._filter_existing_edges(closed_edges)
        if not existing:
            return [], missing, set()

        best_pair, blocked, notes = self._find_detour_endpoints_and_blocked_edges(existing)
        if not best_pair:
            logger.warning("Could not determine detour endpoints for %s", existing)
            return [], missing, set()

        s, t = best_pair
        node_depth = self._get_upstream_nodes(s, depth, excluded={t})

        logger.info(
            "UpstreamBFS: s=%s, t=%s, max_depth=%d — %d candidate origins",
            s, t, depth, len(node_depth),
        )

        seen: set[tuple[str, ...]] = set()
        raw: list[tuple[int, PathResult]] = []

        for origin, d in node_depth.items():
            path = self._dijkstra(origin, t, blocked_edges=blocked)
            if not path or not path.edges:
                continue
            sig = tuple(path.edges)
            if sig in seen:
                continue
            seen.add(sig)
            raw.append((d, path))

        # Deepest origins first; within the same depth, shorter (cheaper) paths first.
        raw.sort(key=lambda x: (-x[0], x[1].total_cost))
        logger.info("UpstreamBFS: %d unique paths found", len(raw))

        plans: list[DeviationPlan] = []
        for rank, (d, path) in enumerate(raw, 1):
            total_length_m = sum(
                self.edges[e].length_m for e in path.edges if e in self.edges
            )
            plans.append(DeviationPlan(
                plan_id=f"plan_{rank}",
                rank=rank,
                depth=d,
                path=path,
                total_length_m=total_length_m,
                source_node=path.nodes[0],
                target_node=t,
                notes=notes,
            ))

        return plans, missing, blocked

    # ------------------------------------------------------------------
    # Network topology helpers
    # ------------------------------------------------------------------

    def get_edge_nodes(self) -> dict[str, tuple[str, str]]:
        """Return a mapping from every edge id to its (from_node, to_node) pair.

        This dict is the only topology information required by the route
        rewriter, keeping that module free of any dependency on
        NetworkGraph.
        """
        return {e_id: (rec.from_node, rec.to_node) for e_id, rec in self.edges.items()}

    # ------------------------------------------------------------------
    # Yen's k-shortest paths
    # ------------------------------------------------------------------

    def k_shortest_paths(
        self,
        source: str,
        target: str,
        k: int,
        *,
        blocked_edges: set[str] | None = None,
    ) -> list[PathResult]:
        """Apply Yen's algorithm to find up to *k* shortest loopless paths.

        Uses a (cost, tie-break counter, path) heap so that
        PathResult objects are never directly compared, avoiding
        fragile list-level ordering.

        Parameters
        ----------
        source:
            Origin junction id.
        target:
            Destination junction id.
        k:
            Maximum number of paths to return.
        blocked_edges:
            Optional set of edge ids excluded from all path searches.

        Returns
        -------
        list[PathResult]
            Up to k loopless paths ordered by total routing cost.
        """
        logger.debug("Yen: %s → %s  k=%d", source, target, k)
        paths, candidate_heap, seen, blocked, counter = self._init_yen_state(
            source, target, k, blocked_edges
        )
        if not paths:
            return []
        for _ in range(1, k):
            base_path = paths[-1]
            self._generate_yen_candidates(
                base_path, paths, target, blocked, seen, candidate_heap, counter
            )
            if not candidate_heap:
                logger.debug("Yen: no more candidates")
                break
            _, _, next_path = heapq.heappop(candidate_heap)
            paths.append(next_path)
            logger.debug(
                "Yen: P%d selected — edges=%s, total=%.3f",
                len(paths), next_path.edges, next_path.total_cost,
            )
        logger.info("Yen: generated %d path(s) from %s to %s", len(paths), source, target)
        return paths

    def _init_yen_state(
        self,
        source: str,
        target: str,
        k: int,
        blocked_edges: set[str] | None,
    ) -> tuple[
        list[PathResult],
        list[tuple[float, int, PathResult]],
        set[tuple[str, ...]],
        set[str],
        Iterator[int],
    ]:
        """Initialise Yen's algorithm state and compute the first (shortest) path."""
        counter: Iterator[int] = itertools.count()
        if k <= 0:
            return [], [], set(), set(), counter
        blocked: set[str] = blocked_edges or set()
        first = self._dijkstra(source, target, blocked_edges=blocked)
        if not first:
            return [], [], set(), blocked, counter
        logger.debug("Yen: P1 — edges=%s, total=%.3f", first.edges, first.total_cost)
        paths = [first]
        candidate_heap: list[tuple[float, int, PathResult]] = []
        seen: set[tuple[str, ...]] = {tuple(first.edges)}
        return paths, candidate_heap, seen, blocked, counter

    def _generate_yen_candidates(
        self,
        base_path: PathResult,
        accepted_paths: list[PathResult],
        target: str,
        blocked_edges: set[str],
        seen: set[tuple[str, ...]],
        candidate_heap: list[tuple[float, int, PathResult]],
        counter: Iterator[int],
    ) -> None:
        """Generate spur paths for Yen's algorithm and push them onto the heap.

        Pushes (total_cost, tie_break_counter, path) tuples onto
        *candidate_heap* so the heap invariant is always maintained without
        comparing PathResult objects directly.
        """
        nodes = base_path.nodes
        for i in range(len(nodes) - 1):
            spur_node = nodes[i]
            root_nodes = nodes[: i + 1]
            root_edges = base_path.edges[:i]
            local_blocked = set(blocked_edges)
            for p in accepted_paths:
                if len(p.nodes) > i and p.nodes[: i + 1] == root_nodes:
                    if i < len(p.edges):
                        local_blocked.add(p.edges[i])
            spur_path = self._dijkstra(spur_node, target, blocked_edges=local_blocked)
            if not spur_path:
                continue
            if any(n in root_nodes[:-1] for n in spur_path.nodes[1:]):
                continue
            candidate = self._combine_root_and_spur(
                base_path, spur_path, root_nodes, root_edges, i
            )
            if not candidate:
                continue
            sig = tuple(candidate.edges)
            if sig in seen:
                continue
            seen.add(sig)
            heapq.heappush(candidate_heap, (candidate.total_cost, next(counter), candidate))

    def _combine_root_and_spur(
        self,
        base_path: PathResult,
        spur_path: PathResult,
        root_nodes: list[str],
        root_edges: list[str],
        idx: int,
    ) -> PathResult | None:
        """Combine root and spur path segments into a full candidate path."""
        combined_nodes = root_nodes[:-1] + spur_path.nodes
        combined_edges = root_edges + spur_path.edges
        if not combined_edges:
            return None
        offset = base_path.cumulative_costs[idx]
        cumulative = base_path.cumulative_costs[:idx] + [
            offset + c for c in spur_path.cumulative_costs
        ]
        return PathResult(
            nodes=combined_nodes, edges=combined_edges, cumulative_costs=cumulative
        )

    # ------------------------------------------------------------------
    # Deviation plan computation (Yen's method — depth-0 plans)
    # ------------------------------------------------------------------

    def _find_detour_endpoints_and_blocked_edges(
        self, closed_edges: list[str]
    ) -> tuple[tuple[str, str] | None, set[str], str | None]:
        """Determine the best source and target junctions for a detour.

        Walks upstream from the closure's source junction until a node with
        more than one outgoing edge is found, then walks downstream from the
        closure's target junction until a node with more than one incoming
        edge is found.  Intermediate edges encountered during these walks are
        added to the blocked set so the router cannot use them as shortcuts.

        When both endpoints need adjustment, the diagnostic note combines
        both messages.

        Parameters
        ----------
        closed_edges:
            Ordered list of existing closed edge ids.

        Returns
        -------
        endpoint_pair : tuple[str, str] or None
            (source_junction, target_junction) for the detour.
        blocked : set[str]
            Edge ids excluded from routing (closed edges + approach edges).
        notes : str or None
            Diagnostic message when either endpoint was adjusted.
        """
        blocked = set(closed_edges)
        note_parts: list[str] = []
        initial_source = self.edges[closed_edges[0]].from_node
        initial_target = self.edges[closed_edges[-1]].to_node

        # Traverse upstream if source has insufficient exits
        current_source = initial_source
        visited_upward_nodes = {current_source}
        while len(self.outgoing.get(current_source, [])) <= 1:
            predecessors = self.incoming.get(current_source, [])
            if not predecessors:
                logger.warning("Reached a graph root at node %s", current_source)
                break
            pred_edge = predecessors[0]
            pred_node = pred_edge.from_node
            if pred_node in visited_upward_nodes:
                logger.warning("Cycle detected while traversing up from %s", current_source)
                break
            blocked.add(pred_edge.edge_id)
            current_source = pred_node
            visited_upward_nodes.add(current_source)
        final_source = current_source
        if final_source != initial_source:
            msg = (
                f"Original source {initial_source} had limited exits; "
                f"rerouting from {final_source}."
            )
            note_parts.append(msg)
            logger.info(msg)

        # Traverse downstream if target has insufficient entries
        current_target = initial_target
        visited_downward_nodes = {current_target}
        while len(self.incoming.get(current_target, [])) <= 1:
            successors = self.outgoing.get(current_target, [])
            if not successors:
                logger.warning("Reached a graph leaf at node %s", current_target)
                break
            succ_edge = successors[0]
            succ_node = succ_edge.to_node
            if succ_node in visited_downward_nodes:
                logger.warning("Cycle detected while traversing down from %s", current_target)
                break
            blocked.add(succ_edge.edge_id)
            current_target = succ_node
            visited_downward_nodes.add(current_target)
        final_target = current_target
        if final_target != initial_target:
            msg = (
                f"Original target {initial_target} had limited entries; "
                f"rerouting to {final_target}."
            )
            note_parts.append(msg)
            logger.info(msg)

        notes = " ".join(note_parts) if note_parts else None
        return (final_source, final_target), blocked, notes

    def compute_deviation_plans(
        self,
        closed_edges: list[str] | tuple[str, ...],
        *,
        k: int = 3,
    ) -> tuple[list[DeviationPlan], list[str], set[str]]:
        """Generate detour plans around user-selected closures using Yen's algorithm.

        All returned plans originate from the closure's immediate source
        junction and are therefore at depth 0.  Use
        compute_upstream_deviation_plans to obtain multi-depth plans.

        Parameters
        ----------
        closed_edges:
            Edge ids to close/block.
        k:
            Maximum number of alternative detour paths to return.

        Returns
        -------
        deviation_plans : list[DeviationPlan]
            Up to k plans sorted by routing cost ascending.
        missing_edges : list[str]
            Closed edges not found in the graph.
        blocked_edges : set[str]
            Full set of edges excluded from detour routing.

        Examples
        --------

            plans, missing, blocked = graph.compute_deviation_plans(
                ["edge_1", "edge_2"], k=3
            )
        """
        existing, missing = self._filter_existing_edges(closed_edges)
        if not existing or k <= 0:
            return [], missing, set()

        best_pair, blocked, notes = self._find_detour_endpoints_and_blocked_edges(existing)
        if not best_pair:
            logger.info("Advanced endpoint search failed; falling back to simple search.")
            blocked_fallback = set(existing)
            best_pair, _ = self._find_best_pair(blocked_fallback, existing)
            if not best_pair:
                return [], missing, set()
            blocked = blocked_fallback
            notes = None

        raw_paths = self.k_shortest_paths(*best_pair, k, blocked_edges=blocked)
        plans = self._build_deviation_plans(raw_paths, best_pair)
        if notes:
            for p in plans:
                p.notes = notes
        return plans, missing, blocked

    def _filter_existing_edges(
        self,
        closed_edges: list[str] | tuple[str, ...],
    ) -> tuple[list[str], list[str]]:
        """Split *closed_edges* into those present in the graph and those missing."""
        existing = [e for e in closed_edges if e in self.edges]
        missing = [e for e in closed_edges if e not in self.edges]
        if missing:
            logger.warning("Edges not found in graph: %s", missing)
        return existing, missing

    def _find_best_pair(
        self,
        blocked: set[str],
        closed_edges: list[str],
    ) -> tuple[tuple[str, str] | None, PathResult | None]:
        """Find the cheapest-path source–target pair among the closed edges.

        Tries every combination of source junctions (from-nodes of closed
        edges) and target junctions (to-nodes of closed edges), excluding
        identical pairs.  Returns the pair whose shortest path has the
        lowest total routing cost.

        Parameters
        ----------
        blocked:
            Edge ids excluded from routing.
        closed_edges:
            Closed edge ids (must all be present in the graph).

        Returns
        -------
        best_pair : tuple[str, str] or None
            (source, target) with the cheapest detour, or None.
        best_path : PathResult or None
            Corresponding shortest path, or None.
        """
        best_pair = None
        best_path = None
        starts = list(dict.fromkeys(self.edges[e].from_node for e in closed_edges))
        ends = list(dict.fromkeys(self.edges[e].to_node for e in closed_edges))
        for s in starts:
            for t in ends:
                if s == t:
                    continue
                path = self.shortest_path(s, t, blocked_edges=blocked)
                if path and (
                    best_path is None or path.total_cost < best_path.total_cost
                ):
                    best_pair, best_path = (s, t), path
        return best_pair, best_path

    def _build_deviation_plans(
        self,
        raw_paths: list[PathResult],
        pair: tuple[str, str],
    ) -> list[DeviationPlan]:
        """Build DeviationPlan objects from raw Yen path results.

        Plans produced here always originate from the fixed closure source
        junction, so their depth is 0 by definition.

        Parameters
        ----------
        raw_paths:
            Paths returned by k_shortest_paths.
        pair:
            (source_junction, target_junction) used for all paths.

        Returns
        -------
        list[DeviationPlan]
            One plan per path, ranked 1-based by routing cost.
        """
        plans = []
        source, target = pair
        for rank, path in enumerate(raw_paths, 1):
            total_length_m = sum(
                self.edges[e].length_m for e in path.edges if e in self.edges
            )
            plans.append(DeviationPlan(
                plan_id=f"plan_{rank}",
                rank=rank,
                depth=0,
                path=path,
                total_length_m=total_length_m,
                source_node=source,
                target_node=target,
            ))
        return plans
