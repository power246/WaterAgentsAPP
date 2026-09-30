from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.hydrology._topology_validation import (
    comid_key,
    coverage_key,
    validate_comid,
)
from app.domain.hydrology.branch_outcome import BranchOutcome
from app.domain.hydrology.branch_outcome_status import BranchOutcomeStatus
from app.domain.hydrology.comid_layer import ComidLayer
from app.domain.hydrology.hydrology_edge import HydrologyEdge
from app.domain.hydrology.hydrology_node import HydrologyNode
from app.domain.hydrology.hydrology_participation import HydrologyParticipation
from app.domain.hydrology.topology_coverage import TopologyCoverage
from app.domain.hydrology.topology_limits import TopologyLimits
from app.domain.hydrology.topology_search_stats import TopologySearchStats
from app.domain.hydrology.waterbody_geometry import WaterbodyGeometry


class HydrologyGraph(DomainModel):
    schema_version: Literal[1] = 1
    target_comid: NonEmptyString
    coverage: TopologyCoverage
    nodes: tuple[HydrologyNode, ...]
    geometries: dict[NonEmptyString, WaterbodyGeometry] = Field(default_factory=dict)
    edges: tuple[HydrologyEdge, ...]
    branch_outcomes: tuple[BranchOutcome, ...]
    execution_layers: tuple[ComidLayer, ...]
    limits: TopologyLimits
    stats: TopologySearchStats

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be integer 1")
        return value

    @field_validator("target_comid")
    @classmethod
    def validate_comid(cls, value: str) -> str:
        return validate_comid(value)

    @field_validator("geometries")
    @classmethod
    def normalize_geometries(
        cls,
        value: dict[str, WaterbodyGeometry],
    ) -> dict[str, WaterbodyGeometry]:
        return dict(sorted(value.items()))

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        if not self.nodes:
            raise ValueError("hydrology graph must contain nodes")
        if not self.branch_outcomes:
            raise ValueError("hydrology graph must contain branch outcomes")
        ensure_unique_tuple(
            tuple(node.comid for node in self.nodes),
            field_name="node COMIDs",
        )
        if self.nodes != tuple(sorted(self.nodes, key=_node_key)):
            raise ValueError("nodes must use stable graph order")
        node_by_comid = {node.comid: node for node in self.nodes}
        referenced_geometry_refs = {
            node.geometry_ref for node in self.nodes if node.geometry_ref is not None
        }
        if set(self.geometries) != referenced_geometry_refs:
            raise ValueError("geometries must match node geometry references exactly")
        for geometry_ref, geometry in self.geometries.items():
            if geometry_ref != geometry.geometry_ref:
                raise ValueError("geometry map keys must match geometry_ref")
        for node in self.nodes:
            if node.geometry_ref is None:
                continue
            geometry = self.geometries[node.geometry_ref]
            if node.comid != geometry.source_comid:
                raise ValueError("node geometry must match node COMID")
            if node.geometry_summary != geometry.summary():
                raise ValueError("node geometry summary must match full geometry")
            if node.point != geometry.representative_point:
                raise ValueError("node point must match geometry representative point")
        targets = tuple(node for node in self.nodes if node.is_target)
        if len(targets) != 1 or targets[0].comid != self.target_comid:
            raise ValueError("graph must contain exactly its declared target")
        target = targets[0]
        if target.depth != 0 or target.cumulative_distance_km != 0:
            raise ValueError("target node must be the search origin")
        if target.participation is not HydrologyParticipation.ACTIVE:
            raise ValueError("target node must remain active")

        edge_pairs = tuple(
            (edge.upstream_comid, edge.downstream_comid) for edge in self.edges
        )
        ensure_unique_tuple(edge_pairs, field_name="hydrology edges")
        if any(
            upstream not in node_by_comid or downstream not in node_by_comid
            for upstream, downstream in edge_pairs
        ):
            raise ValueError("edge endpoints must reference graph nodes")
        if self.edges != tuple(sorted(self.edges, key=_edge_key)):
            raise ValueError("edges must use stable graph order")
        self._validate_acyclic_and_target_reachable(edge_pairs, node_by_comid)

        ensure_unique_tuple(self.branch_outcomes, field_name="branch_outcomes")
        if self.branch_outcomes != tuple(
            sorted(self.branch_outcomes, key=_outcome_key)
        ):
            raise ValueError("branch_outcomes must use stable graph order")
        active_comids = {self.target_comid}
        active_edges: set[tuple[str, str]] = set()
        edge_pair_set = set(edge_pairs)
        for outcome in self.branch_outcomes:
            if outcome.path_to_target[-1] != self.target_comid:
                raise ValueError("outcome paths must end at target")
            if any(comid not in node_by_comid for comid in outcome.path_to_target):
                raise ValueError("outcome paths must reference graph nodes")
            path_edges = set(zip(outcome.path_to_target, outcome.path_to_target[1:]))
            if not path_edges.issubset(edge_pair_set):
                raise ValueError("outcome paths must follow graph edges")
            if outcome.outcome is BranchOutcomeStatus.QUALIFIED:
                stop = node_by_comid[outcome.stop_comid]
                if not stop.is_inlet or outcome.coverage_unit not in stop.qualified_units:
                    raise ValueError("qualified outcomes must reference a qualified inlet")
                active_comids.update(outcome.path_to_target)
                active_edges.update(path_edges)

        for node in self.nodes:
            expected = (
                HydrologyParticipation.ACTIVE
                if node.comid in active_comids
                else HydrologyParticipation.EXCLUDED
            )
            if node.participation is not expected:
                raise ValueError("node participation must match qualified path union")
        if any(
            edge.participating
            != ((edge.upstream_comid, edge.downstream_comid) in active_edges)
            for edge in self.edges
        ):
            raise ValueError("edge participation must match qualified path union")

        self._validate_layers(node_by_comid, active_comids, active_edges)
        self._validate_coverage()
        self._validate_stats()
        return self

    @staticmethod
    def _validate_acyclic_and_target_reachable(
        edge_pairs: tuple[tuple[str, str], ...],
        node_by_comid: dict[str, HydrologyNode],
    ) -> None:
        downstreams: dict[str, set[str]] = {
            comid: set() for comid in node_by_comid
        }
        indegree = {comid: 0 for comid in node_by_comid}
        for upstream, downstream in edge_pairs:
            downstreams[upstream].add(downstream)
            indegree[downstream] += 1
        ready = sorted(
            (comid for comid, count in indegree.items() if count == 0),
            key=comid_key,
        )
        visited: list[str] = []
        while ready:
            current = ready.pop(0)
            visited.append(current)
            for downstream in sorted(downstreams[current], key=comid_key):
                indegree[downstream] -= 1
                if indegree[downstream] == 0:
                    ready.append(downstream)
                    ready.sort(key=comid_key)
        if len(visited) != len(node_by_comid):
            raise ValueError("hydrology graph must be acyclic")

        target = next(node.comid for node in node_by_comid.values() if node.is_target)
        memo: dict[str, bool] = {target: True}

        def reaches_target(comid: str) -> bool:
            if comid in memo:
                return memo[comid]
            memo[comid] = False
            memo[comid] = any(
                reaches_target(item) for item in downstreams[comid]
            )
            return memo[comid]

        if any(not reaches_target(comid) for comid in node_by_comid):
            raise ValueError("every explored node must flow toward target")

    def _validate_layers(
        self,
        node_by_comid: dict[str, HydrologyNode],
        active_comids: set[str],
        active_edges: set[tuple[str, str]],
    ) -> None:
        if not self.execution_layers:
            raise ValueError("execution_layers must not be empty")
        flattened = tuple(
            comid for layer in self.execution_layers for comid in layer.root
        )
        ensure_unique_tuple(flattened, field_name="execution layer COMIDs")
        if set(flattened) != active_comids:
            raise ValueError("execution layers must contain every active node once")
        if self.execution_layers[-1].root != (self.target_comid,):
            raise ValueError("target must be the final execution layer")
        layer_index = {
            comid: index
            for index, layer in enumerate(self.execution_layers)
            for comid in layer.root
        }
        if any(
            layer_index[upstream] >= layer_index[downstream]
            for upstream, downstream in active_edges
        ):
            raise ValueError("execution layers must follow upstream dependency order")
        for layer in self.execution_layers:
            expected = tuple(
                sorted(layer.root, key=lambda item: _node_key(node_by_comid[item]))
            )
            if layer.root != expected:
                raise ValueError("execution layer COMIDs must use stable node order")

    def _validate_coverage(self) -> None:
        qualified = sum(
            outcome.outcome is BranchOutcomeStatus.QUALIFIED
            for outcome in self.branch_outcomes
        )
        if qualified == len(self.branch_outcomes):
            expected = TopologyCoverage.COMPLETE
        elif qualified:
            expected = TopologyCoverage.PARTIAL
        else:
            expected = TopologyCoverage.NONE
        if self.coverage is not expected:
            raise ValueError("coverage must match branch outcomes")

    def _validate_stats(self) -> None:
        expected = TopologySearchStats(
            discovered_nodes=len(self.nodes),
            queried_nodes=self.stats.queried_nodes,
            max_depth=max(node.depth for node in self.nodes),
            max_distance_km=max(
                node.cumulative_distance_km for node in self.nodes
            ),
            qualified_inlets=sum(node.is_inlet for node in self.nodes),
            excluded_nodes=sum(
                node.participation is HydrologyParticipation.EXCLUDED
                for node in self.nodes
            ),
        )
        if self.stats != expected:
            raise ValueError("topology stats must match graph contents")
        if self.stats.queried_nodes > self.stats.discovered_nodes:
            raise ValueError("queried_nodes must not exceed discovered_nodes")
        if self.stats.discovered_nodes > self.limits.max_nodes:
            raise ValueError("graph must not exceed max_nodes")
        if self.stats.max_depth > self.limits.max_hops:
            raise ValueError("graph must not exceed max_hops")
        if self.stats.max_distance_km > self.limits.max_distance_km:
            raise ValueError("graph must not exceed max_distance_km")


def _node_key(node: HydrologyNode) -> tuple[int, float, int, str]:
    return (
        node.depth,
        node.cumulative_distance_km,
        *comid_key(node.comid),
    )


def _edge_key(edge: HydrologyEdge) -> tuple[int, str, int, str]:
    return (*comid_key(edge.upstream_comid), *comid_key(edge.downstream_comid))


def _outcome_key(
    outcome: BranchOutcome,
) -> tuple[object, ...]:
    return (
        coverage_key(outcome.coverage_unit),
        *comid_key(outcome.branch_root_comid),
        len(outcome.path_to_target),
        tuple(comid_key(comid) for comid in outcome.path_to_target),
        outcome.outcome.value,
        *_optional_comid_key(outcome.blocked_upstream_comid),
        outcome.reason.value,
    )


def _optional_comid_key(comid: str | None) -> tuple[int, str]:
    return (-1, "") if comid is None else comid_key(comid)
