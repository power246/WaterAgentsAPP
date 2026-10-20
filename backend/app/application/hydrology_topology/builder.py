from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from math import isclose, isfinite
from typing import TYPE_CHECKING, TypeVar

from pydantic import ValidationError

from app.application.hydrology_topology.errors import TopologyInvariantError
from app.application.query_understanding import AnalysisContext
from app.application.station_availability import build_station_availability_request
from app.domain import (
    BranchOutcome,
    BranchOutcomeReason,
    BranchOutcomeStatus,
    ComidLayer,
    CoverageUnit,
    ExclusionReasonCode,
    GeoPoint,
    HydrologyEdge,
    HydrologyExclusionReason,
    HydrologyGraph,
    HydrologyNode,
    HydrologyParticipation,
    ParameterCapabilityRegistry,
    ParameterKey,
    ParameterProvider,
    RiverSegment,
    StationAvailability,
    StationAvailabilityRequest,
    TopologyLimits,
    TopologySearchStats,
    TopologySeriesRef,
    TopologyStationRef,
    WaterbodyGeometry,
)
from app.domain.hydrology.topology_coverage import derive_topology_coverage
from app.ports import (
    InvalidPortResponseError,
    PortError,
    PortTimeoutError,
    StationInventoryPort,
    TopologyDataPort,
)

ResultT = TypeVar("ResultT")
PROVIDER = "hydrology_topology"
logger = logging.getLogger("wateragents.hydrology_topology")

if TYPE_CHECKING:
    from app.application.evidence_tools.numeric_methods import NumericMethodRegistry


@dataclass(frozen=True)
class _PathState:
    branch_root_comid: str
    comid: str
    path_to_target: tuple[str, ...]
    depth: int
    cumulative_distance_km: float
    coverage_units: tuple[CoverageUnit, ...]
    historical_candidates: tuple[CoverageUnit, ...] = ()


@dataclass
class _NodeRecord:
    point: GeoPoint | None
    geometry: WaterbodyGeometry | None
    depth: int
    cumulative_distance_km: float


@dataclass(frozen=True)
class _Expansion:
    parent: _PathState
    segment: RiverSegment
    branch_root_comid: str
    depth: int
    cumulative_distance_km: float


@dataclass(frozen=True)
class _ExpansionDecision:
    expansion: _Expansion
    limit_reason: BranchOutcomeReason | None = None


class HydrologyGraphBuilder:
    def __init__(
        self,
        *,
        topology: TopologyDataPort,
        station_inventory: StationInventoryPort,
        limits: TopologyLimits,
        max_concurrency: int,
        stage_timeout_seconds: float,
        prediction_baseline_hours: int = 48,
        numeric_registry: NumericMethodRegistry | None = None,
        capability_registry: ParameterCapabilityRegistry | None = None,
    ) -> None:
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or max_concurrency <= 0
        ):
            raise ValueError("max_concurrency must be a positive integer")
        if (
            isinstance(stage_timeout_seconds, bool)
            or not isinstance(stage_timeout_seconds, (int, float))
            or not isfinite(stage_timeout_seconds)
            or stage_timeout_seconds <= 0
        ):
            raise ValueError("stage_timeout_seconds must be finite and positive")
        if (
            isinstance(prediction_baseline_hours, bool)
            or not isinstance(prediction_baseline_hours, int)
            or prediction_baseline_hours <= 0
        ):
            raise ValueError("prediction_baseline_hours must be a positive integer")
        self._topology = topology
        self._station_inventory = station_inventory
        self._limits = limits
        self._max_concurrency = max_concurrency
        self._stage_timeout_seconds = float(stage_timeout_seconds)
        self._prediction_baseline_hours = prediction_baseline_hours
        if numeric_registry is not None and capability_registry is None:
            raise ValueError("numeric methods require a capability registry")
        self._numeric_registry = numeric_registry
        self._capability_registry = capability_registry

    async def build(
        self,
        context: AnalysisContext,
        *,
        deadline: float | None = None,
    ) -> HydrologyGraph:
        loop = asyncio.get_running_loop()
        stage_deadline = loop.time() + self._stage_timeout_seconds
        effective_deadline = (
            stage_deadline if deadline is None else min(stage_deadline, deadline)
        )
        semaphore = asyncio.Semaphore(self._max_concurrency)
        base_request = build_station_availability_request(
            comid=context.target.comid,
            target_parameters=tuple(item.key for item in context.params_used),
            supporting_parameters=tuple(item.key for item in context.supporting_params),
            evidence_parameters=context.evidence_rule_parameters,
            time_segments=context.time_segments,
            time_resolution=context.time_resolution,
            analysis_timezone=context.analysis_timezone,
            prediction_baseline_hours=self._prediction_baseline_hours,
        )
        method_input_parameters = self._method_input_parameters(context)
        auxiliary_units = tuple(
            sorted(
                {
                    CoverageUnit(
                        parameter=parameter,
                        segment_index=unit.segment_index,
                        window_index=unit.window_index,
                    )
                    for unit in base_request.coverage_units
                    for parameter in method_input_parameters
                },
                key=_coverage_key,
            )
        )
        availability_request = (
            StationAvailabilityRequest(
                comid=base_request.comid,
                coverage_units=tuple(
                    sorted(
                        {*base_request.coverage_units, *auxiliary_units},
                        key=_coverage_key,
                    )
                ),
                historical_ranges=base_request.historical_ranges,
                prediction_baseline_ranges=base_request.prediction_baseline_ranges,
            )
            if auxiliary_units
            else base_request
        )
        historical_segment_indexes = frozenset(
            item.segment_index for item in base_request.historical_ranges
        )
        target_comid = context.target.comid
        target_segment = await self._external_call(
            lambda: self._topology.get_segment(
                target_comid,
                deadline=effective_deadline,
            ),
            semaphore=semaphore,
            deadline=effective_deadline,
            operation="get_segment",
            comid=target_comid,
        )
        if target_segment is not None and target_segment.comid != target_comid:
            error = InvalidPortResponseError(
                "Target segment response referenced a different COMID.",
                provider=PROVIDER,
                operation="get_segment",
            )
            self._log_port_failure(error, operation="get_segment", comid=target_comid)
            raise error
        nodes: dict[str, _NodeRecord] = {
            target_comid: _NodeRecord(
                point=(
                    target_segment.location
                    if target_segment is not None
                    else context.target.point
                ),
                geometry=(
                    target_segment.geometry
                    if target_segment is not None
                    else None
                ),
                depth=0,
                cumulative_distance_km=0.0,
            )
        }
        edges: dict[tuple[str, str], float] = {}
        availability_cache: dict[str, tuple[StationAvailability, ...]] = {}
        upstream_cache: dict[str, tuple[RiverSegment, ...]] = {}
        outcomes: set[BranchOutcome] = set()
        frontier = (
            _PathState(
                branch_root_comid=target_comid,
                comid=target_comid,
                path_to_target=(target_comid,),
                depth=0,
                cumulative_distance_km=0.0,
                coverage_units=base_request.coverage_units,
            ),
        )

        while frontier:
            ordered_frontier = tuple(sorted(set(frontier), key=_state_key))
            grouped = self._group_frontier(ordered_frontier)
            evaluations = await self._evaluate_frontier(
                grouped,
                availability_request=availability_request,
                historical_segment_indexes=historical_segment_indexes,
                availability_cache=availability_cache,
                upstream_cache=upstream_cache,
                semaphore=semaphore,
                deadline=effective_deadline,
            )
            expansions: list[_Expansion] = []
            for comid in sorted(grouped, key=_comid_key):
                states = grouped[comid]
                availability, upstreams = evaluations[comid]
                covered = {
                    unit
                    for item in availability
                    for unit in item.covered_units
                }
                for state in states:
                    historical_candidates = set(state.historical_candidates)
                    qualified = tuple(
                        unit for unit in state.coverage_units if unit in covered
                    )
                    for unit in qualified:
                        outcomes.add(
                            self._outcome(
                                state,
                                unit,
                                BranchOutcomeStatus.QUALIFIED,
                                (
                                    BranchOutcomeReason.TARGET_STATION_QUALIFIED
                                    if state.comid == target_comid
                                    else BranchOutcomeReason.STATION_QUALIFIED
                                ),
                            )
                        )
                    remaining = tuple(
                        unit
                        for unit in state.coverage_units
                        if unit not in covered
                        or (
                            unit.segment_index in historical_segment_indexes
                            and unit not in historical_candidates
                        )
                    )
                    if not remaining:
                        continue

                    remaining_state = _PathState(
                        branch_root_comid=state.branch_root_comid,
                        comid=state.comid,
                        path_to_target=state.path_to_target,
                        depth=state.depth,
                        cumulative_distance_km=state.cumulative_distance_km,
                        coverage_units=remaining,
                        historical_candidates=tuple(
                            sorted(
                                historical_candidates.union(
                                    unit
                                    for unit in qualified
                                    if unit.segment_index
                                    in historical_segment_indexes
                                ),
                                key=_coverage_key,
                            )
                        ),
                    )

                    limit_reason = self._current_limit_reason(state)
                    if limit_reason is not None:
                        self._add_terminal_outcomes(
                            outcomes,
                            remaining_state,
                            remaining,
                            BranchOutcomeStatus.SEARCH_LIMIT_REACHED,
                            limit_reason,
                        )
                        continue
                    if not upstreams:
                        self._add_terminal_outcomes(
                            outcomes,
                            remaining_state,
                            remaining,
                            BranchOutcomeStatus.NO_QUALIFYING_SOURCE,
                            BranchOutcomeReason.NATURAL_HEADWATER,
                        )
                        continue

                    for segment in upstreams:
                        decision = self._decide_expansion(
                            remaining_state,
                            segment,
                            target_comid=target_comid,
                            sibling_count=len(upstreams),
                        )
                        if decision.limit_reason is not None:
                            self._add_blocked_expansion_outcomes(
                                outcomes,
                                decision.expansion,
                                decision.limit_reason,
                            )
                            continue
                        expansions.append(decision.expansion)

            next_frontier: set[_PathState] = set()
            for expansion in sorted(expansions, key=_expansion_key):
                upstream = expansion.segment.comid
                if upstream not in nodes and len(nodes) >= self._limits.max_nodes:
                    self._add_blocked_expansion_outcomes(
                        outcomes,
                        expansion,
                        BranchOutcomeReason.MAX_NODES_REACHED,
                    )
                    continue
                self._record_node(nodes, expansion)
                self._record_edge(edges, expansion)
                next_frontier.add(
                    _PathState(
                        branch_root_comid=expansion.branch_root_comid,
                        comid=upstream,
                        path_to_target=(
                            upstream,
                            *expansion.parent.path_to_target,
                        ),
                        depth=expansion.depth,
                        cumulative_distance_km=expansion.cumulative_distance_km,
                        coverage_units=expansion.parent.coverage_units,
                        historical_candidates=expansion.parent.historical_candidates,
                    )
                )
            frontier = tuple(next_frontier)

        return self._assemble_graph(
            context=context,
            nodes=nodes,
            edges=edges,
            outcomes=outcomes,
            availability_cache=availability_cache,
            auxiliary_units=auxiliary_units,
        )

    def _method_input_parameters(
        self,
        context: AnalysisContext,
    ) -> tuple[ParameterKey, ...]:
        registry = self._numeric_registry
        capabilities = self._capability_registry
        if registry is None or capabilities is None:
            return ()
        modes = {segment.mode.value for segment in context.time_segments}
        roots = {
            item.key for item in (*context.params_used, *context.supporting_params)
        }
        visited: set[ParameterKey] = set()
        inputs: set[ParameterKey] = set()

        def visit(parameter: ParameterKey) -> None:
            if parameter in visited:
                return
            visited.add(parameter)
            for method in registry.for_output(parameter):
                if not modes.intersection(method.allowed_modes):
                    continue
                for input_ in method.inputs:
                    if input_.parameter == parameter:
                        continue
                    inputs.add(input_.parameter)
                    visit(input_.parameter)

        for parameter in roots:
            visit(parameter)
        return tuple(
            sorted(
                (
                    parameter
                    for parameter in inputs - roots
                    if (
                        capability := capabilities.get(parameter)
                    ) is not None
                    and any(
                        policy.direct_observation
                        and policy.provider in capability.availability_providers
                        and policy.provider in capability.observation_providers
                        for policy in capability.channel_policies
                    )
                ),
                key=lambda item: (item.code_system, item.code),
            )
        )

    async def _evaluate_frontier(
        self,
        grouped: dict[str, tuple[_PathState, ...]],
        *,
        availability_request: StationAvailabilityRequest,
        historical_segment_indexes: frozenset[int],
        availability_cache: dict[str, tuple[StationAvailability, ...]],
        upstream_cache: dict[str, tuple[RiverSegment, ...]],
        semaphore: asyncio.Semaphore,
        deadline: float,
    ) -> dict[str, tuple[tuple[StationAvailability, ...], tuple[RiverSegment, ...]]]:
        ordered_comids = tuple(sorted(grouped, key=_comid_key))

        async def evaluate(
            comid: str,
        ) -> tuple[str, tuple[StationAvailability, ...], tuple[RiverSegment, ...]]:
            availability = availability_cache.get(comid)
            if availability is None:
                request = StationAvailabilityRequest(
                    comid=comid,
                    coverage_units=availability_request.coverage_units,
                    historical_ranges=availability_request.historical_ranges,
                    prediction_baseline_ranges=availability_request.prediction_baseline_ranges,
                )
                availability = await self._external_call(
                    lambda: self._station_inventory.check_availability(
                        request,
                        deadline=deadline,
                    ),
                    semaphore=semaphore,
                    deadline=deadline,
                    operation="check_availability",
                    comid=comid,
                )
                try:
                    availability = self._normalize_availability(comid, availability)
                except PortError as error:
                    self._log_port_failure(
                        error, operation="check_availability", comid=comid
                    )
                    raise
                availability_cache[comid] = availability

            covered = {
                unit for item in availability for unit in item.covered_units
            }
            needs_upstreams = any(
                any(
                    unit not in covered
                    or (
                        unit.segment_index in historical_segment_indexes
                        and unit not in state.historical_candidates
                    )
                    for unit in state.coverage_units
                )
                and self._current_limit_reason(state) is None
                for state in grouped[comid]
            )
            upstreams = upstream_cache.get(comid, ())
            if needs_upstreams and comid not in upstream_cache:
                upstreams = await self._external_call(
                    lambda: self._topology.get_direct_upstreams(
                        comid,
                        self._limits.max_distance_km,
                        deadline=deadline,
                    ),
                    semaphore=semaphore,
                    deadline=deadline,
                    operation="get_direct_upstreams",
                    comid=comid,
                )
                try:
                    upstreams = self._normalize_upstreams(comid, upstreams)
                except PortError as error:
                    self._log_port_failure(
                        error, operation="get_direct_upstreams", comid=comid
                    )
                    raise
                upstream_cache[comid] = upstreams
            return comid, availability, upstreams

        tasks = [asyncio.create_task(evaluate(comid)) for comid in ordered_comids]
        try:
            results = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return {
            comid: (availability, upstreams)
            for comid, availability, upstreams in results
        }

    async def _external_call(
        self,
        call: Callable[[], Awaitable[ResultT]],
        *,
        semaphore: asyncio.Semaphore,
        deadline: float,
        operation: str,
        comid: str,
    ) -> ResultT:
        try:
            self._ensure_deadline(deadline, operation)
            async with semaphore:
                self._ensure_deadline(deadline, operation)
                async with asyncio.timeout_at(deadline):
                    return await call()
        except PortError as error:
            self._log_port_failure(error, operation=operation, comid=comid)
            # Normalize provider-specific sub-operations at this application
            # boundary. Public workflow errors distinguish topology lookup from
            # station qualification, not the adapter method that happened to
            # fail inside either operation.
            error.operation = operation
            raise
        except TimeoutError:
            error = PortTimeoutError(
                "Hydrology topology deadline was exhausted.",
                provider=PROVIDER,
                operation=operation,
            )
            self._log_port_failure(error, operation=operation, comid=comid)
            raise error from None

    @staticmethod
    def _log_port_failure(error: PortError, *, operation: str, comid: str) -> None:
        logger.warning(
            "Hydrology topology %s failed for COMID %s: %s",
            operation,
            comid,
            error,
            extra={
                "event_name": "topology_provider_failed",
                "operation": operation,
                "comid": comid,
                "provider": error.provider,
                "provider_operation": error.operation,
                "provider_error": type(error).__name__,
                "provider_status": error.status_code,
                "provider_retry_after_seconds": getattr(
                    error, "retry_after_seconds", None
                ),
            },
        )

    def _current_limit_reason(
        self,
        state: _PathState,
    ) -> BranchOutcomeReason | None:
        distance = state.cumulative_distance_km >= self._limits.max_distance_km
        hops = state.depth >= self._limits.max_hops
        if distance and hops:
            return BranchOutcomeReason.MULTIPLE_LIMITS_REACHED
        if distance:
            return BranchOutcomeReason.MAX_DISTANCE_REACHED
        if hops:
            return BranchOutcomeReason.MAX_HOPS_REACHED
        return None

    def _decide_expansion(
        self,
        state: _PathState,
        segment: RiverSegment,
        *,
        target_comid: str,
        sibling_count: int,
    ) -> _ExpansionDecision:
        self._validate_expansion(state, segment)
        distance = state.cumulative_distance_km + self._edge_distance(segment)
        branch_root = (
            segment.comid
            if state.comid == target_comid or sibling_count > 1
            else state.branch_root_comid
        )
        expansion = _Expansion(
            parent=state,
            segment=segment,
            branch_root_comid=branch_root,
            depth=state.depth + 1,
            cumulative_distance_km=distance,
        )
        return _ExpansionDecision(
            expansion=expansion,
            limit_reason=(
                BranchOutcomeReason.MAX_DISTANCE_REACHED
                if distance > self._limits.max_distance_km
                else None
            ),
        )

    @staticmethod
    def _group_frontier(
        frontier: tuple[_PathState, ...],
    ) -> dict[str, tuple[_PathState, ...]]:
        grouped: dict[str, list[_PathState]] = {}
        for state in frontier:
            grouped.setdefault(state.comid, []).append(state)
        return {
            comid: tuple(sorted(states, key=_state_key))
            for comid, states in grouped.items()
        }

    @staticmethod
    def _normalize_availability(
        comid: str,
        values: tuple[StationAvailability, ...],
    ) -> tuple[StationAvailability, ...]:
        by_station: dict[str, StationAvailability] = {}
        for item in values:
            if item.comid != comid:
                raise InvalidPortResponseError(
                    "Station availability referenced a different COMID.",
                    provider=PROVIDER,
                    operation="check_availability",
                )
            previous = by_station.get(item.station.station_id)
            if previous is not None and previous != item:
                raise InvalidPortResponseError(
                    "Station availability contained conflicting station records.",
                    provider=PROVIDER,
                    operation="check_availability",
                )
            by_station[item.station.station_id] = item
        return tuple(by_station[key] for key in sorted(by_station))

    @staticmethod
    def _normalize_upstreams(
        downstream_comid: str,
        values: tuple[RiverSegment, ...],
    ) -> tuple[RiverSegment, ...]:
        by_comid: dict[str, RiverSegment] = {}
        for item in values:
            if downstream_comid not in item.downstream_comids:
                raise InvalidPortResponseError(
                    "Direct upstream record did not reference its downstream COMID.",
                    provider=PROVIDER,
                    operation="get_direct_upstreams",
                )
            if item.distance_to_downstream_km is None:
                raise InvalidPortResponseError(
                    "Direct upstream record omitted edge distance.",
                    provider=PROVIDER,
                    operation="get_direct_upstreams",
                )
            previous = by_comid.get(item.comid)
            if previous is not None and previous != item:
                raise InvalidPortResponseError(
                    "Direct upstream response contained conflicting duplicates.",
                    provider=PROVIDER,
                    operation="get_direct_upstreams",
                )
            by_comid[item.comid] = item
        return tuple(by_comid[key] for key in sorted(by_comid, key=_comid_key))

    @staticmethod
    def _validate_expansion(state: _PathState, segment: RiverSegment) -> None:
        if segment.comid in state.path_to_target:
            raise TopologyInvariantError("topology contains a cycle or self-edge")

    @staticmethod
    def _edge_distance(segment: RiverSegment) -> float:
        distance = segment.distance_to_downstream_km
        if distance is None or not isfinite(distance) or distance <= 0:
            raise InvalidPortResponseError(
                "Direct upstream record contained an invalid edge distance.",
                provider=PROVIDER,
                operation="get_direct_upstreams",
            )
        return distance

    @staticmethod
    def _record_node(
        nodes: dict[str, _NodeRecord],
        expansion: _Expansion,
    ) -> None:
        comid = expansion.segment.comid
        previous = nodes.get(comid)
        if previous is None:
            nodes[comid] = _NodeRecord(
                point=expansion.segment.location,
                geometry=expansion.segment.geometry,
                depth=expansion.depth,
                cumulative_distance_km=expansion.cumulative_distance_km,
            )
            return
        if previous.point != expansion.segment.location:
            raise TopologyInvariantError(
                "topology returned inconsistent metadata for a shared COMID"
            )
        if previous.geometry != expansion.segment.geometry:
            raise TopologyInvariantError(
                "topology returned inconsistent geometry for a shared COMID"
            )
        previous_key = (
            previous.depth,
            previous.cumulative_distance_km,
            *_comid_key(comid),
        )
        candidate_key = (
            expansion.depth,
            expansion.cumulative_distance_km,
            *_comid_key(comid),
        )
        if candidate_key < previous_key:
            previous.depth = expansion.depth
            previous.cumulative_distance_km = expansion.cumulative_distance_km

    @staticmethod
    def _record_edge(
        edges: dict[tuple[str, str], float],
        expansion: _Expansion,
    ) -> None:
        pair = (expansion.segment.comid, expansion.parent.comid)
        distance = HydrologyGraphBuilder._edge_distance(expansion.segment)
        previous = edges.get(pair)
        if previous is not None and not isclose(
            previous,
            distance,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise TopologyInvariantError(
                "topology returned inconsistent distances for a shared edge"
            )
        edges[pair] = distance

    @staticmethod
    def _outcome(
        state: _PathState,
        unit: CoverageUnit,
        status: BranchOutcomeStatus,
        reason: BranchOutcomeReason,
        *,
        blocked_upstream_comid: str | None = None,
    ) -> BranchOutcome:
        return BranchOutcome(
            branch_root_comid=state.branch_root_comid,
            coverage_unit=unit,
            outcome=status,
            stop_comid=state.comid,
            path_to_target=state.path_to_target,
            reason=reason,
            blocked_upstream_comid=blocked_upstream_comid,
        )

    def _add_terminal_outcomes(
        self,
        outcomes: set[BranchOutcome],
        state: _PathState,
        units: Iterable[CoverageUnit],
        status: BranchOutcomeStatus,
        reason: BranchOutcomeReason,
    ) -> None:
        for unit in units:
            outcomes.add(
                self._outcome(
                    state,
                    unit,
                    (
                        BranchOutcomeStatus.BACKUP_SEARCH_ENDED
                        if unit in state.historical_candidates
                        else status
                    ),
                    reason,
                )
            )

    def _add_blocked_expansion_outcomes(
        self,
        outcomes: set[BranchOutcome],
        expansion: _Expansion,
        reason: BranchOutcomeReason,
    ) -> None:
        for unit in expansion.parent.coverage_units:
            outcomes.add(
                self._outcome(
                    expansion.parent,
                    unit,
                    (
                        BranchOutcomeStatus.BACKUP_SEARCH_ENDED
                        if unit in expansion.parent.historical_candidates
                        else BranchOutcomeStatus.SEARCH_LIMIT_REACHED
                    ),
                    reason,
                    blocked_upstream_comid=expansion.segment.comid,
                )
            )

    def _assemble_graph(
        self,
        *,
        context: AnalysisContext,
        nodes: dict[str, _NodeRecord],
        edges: dict[tuple[str, str], float],
        outcomes: set[BranchOutcome],
        availability_cache: dict[str, tuple[StationAvailability, ...]],
        auxiliary_units: tuple[CoverageUnit, ...],
    ) -> HydrologyGraph:
        ordered_outcomes = tuple(sorted(outcomes, key=_outcome_key))
        active_comids = {context.target.comid}
        active_edge_pairs: set[tuple[str, str]] = set()
        qualified_by_comid: dict[str, set[CoverageUnit]] = {}
        for outcome in ordered_outcomes:
            if outcome.outcome is not BranchOutcomeStatus.QUALIFIED:
                continue
            active_comids.update(outcome.path_to_target)
            active_edge_pairs.update(
                zip(outcome.path_to_target, outcome.path_to_target[1:])
            )
            qualified_by_comid.setdefault(outcome.stop_comid, set()).add(
                outcome.coverage_unit
            )

        exclusions: dict[str, set[HydrologyExclusionReason]] = {}
        for outcome in ordered_outcomes:
            if outcome.outcome is BranchOutcomeStatus.QUALIFIED:
                continue
            code = (
                ExclusionReasonCode.NO_QUALIFYING_SOURCE
                if outcome.outcome is BranchOutcomeStatus.NO_QUALIFYING_SOURCE
                else ExclusionReasonCode.BACKUP_SEARCH_ENDED
                if outcome.outcome is BranchOutcomeStatus.BACKUP_SEARCH_ENDED
                else ExclusionReasonCode.SEARCH_LIMIT_REACHED
            )
            reason = HydrologyExclusionReason(
                code=code,
                branch_root_comid=outcome.branch_root_comid,
                coverage_unit=outcome.coverage_unit,
                stop_comid=outcome.stop_comid,
                blocked_upstream_comid=outcome.blocked_upstream_comid,
            )
            for comid in outcome.path_to_target:
                if comid not in active_comids:
                    exclusions.setdefault(comid, set()).add(reason)

        graph_nodes: list[HydrologyNode] = []
        for comid, record in nodes.items():
            participation = (
                HydrologyParticipation.ACTIVE
                if comid in active_comids
                else HydrologyParticipation.EXCLUDED
            )
            qualified_units = tuple(
                sorted(qualified_by_comid.get(comid, set()), key=_coverage_key)
            )
            graph_nodes.append(
                HydrologyNode(
                    comid=comid,
                    point=record.point,
                    geometry_ref=(
                        record.geometry.geometry_ref
                        if record.geometry is not None
                        else None
                    ),
                    geometry_summary=(
                        record.geometry.summary()
                        if record.geometry is not None
                        else None
                    ),
                    depth=record.depth,
                    cumulative_distance_km=record.cumulative_distance_km,
                    is_target=comid == context.target.comid,
                    is_inlet=bool(qualified_units),
                    participation=participation,
                    qualified_units=qualified_units,
                    station_refs=self._station_refs(
                        availability_cache.get(comid, ()),
                        qualified_units,
                        (
                            auxiliary_units
                            if participation is HydrologyParticipation.ACTIVE
                            else ()
                        ),
                    ),
                    excluded_reasons=tuple(
                        sorted(exclusions.get(comid, set()), key=_exclusion_key)
                    ),
                )
            )
        ordered_nodes = tuple(sorted(graph_nodes, key=_node_key))
        ordered_edges = tuple(
            sorted(
                (
                    HydrologyEdge(
                        upstream_comid=upstream,
                        downstream_comid=downstream,
                        distance_km=distance,
                        participating=(upstream, downstream) in active_edge_pairs,
                    )
                    for (upstream, downstream), distance in edges.items()
                ),
                key=_edge_key,
            )
        )
        layers = self._execution_layers(
            ordered_nodes,
            active_edge_pairs,
            target_comid=context.target.comid,
        )
        coverage = derive_topology_coverage(ordered_outcomes)
        stats = TopologySearchStats(
            discovered_nodes=len(ordered_nodes),
            queried_nodes=len(availability_cache),
            max_depth=max(node.depth for node in ordered_nodes),
            max_distance_km=max(
                node.cumulative_distance_km for node in ordered_nodes
            ),
            qualified_inlets=sum(node.is_inlet for node in ordered_nodes),
            excluded_nodes=sum(
                node.participation is HydrologyParticipation.EXCLUDED
                for node in ordered_nodes
            ),
        )
        try:
            return HydrologyGraph(
                target_comid=context.target.comid,
                coverage=coverage,
                nodes=ordered_nodes,
                geometries={
                    record.geometry.geometry_ref: record.geometry
                    for record in nodes.values()
                    if record.geometry is not None
                },
                edges=ordered_edges,
                branch_outcomes=ordered_outcomes,
                execution_layers=layers,
                limits=self._limits,
                stats=stats,
            )
        except ValidationError as error:
            raise TopologyInvariantError("constructed hydrology graph is invalid") from error

    @staticmethod
    def _station_refs(
        availability: tuple[StationAvailability, ...],
        qualified_units: tuple[CoverageUnit, ...],
        auxiliary_units: tuple[CoverageUnit, ...] = (),
    ) -> tuple[TopologyStationRef, ...]:
        selected = {*qualified_units, *auxiliary_units}
        grouped: dict[
            tuple[ParameterProvider, str],
            dict[tuple[str, ParameterKey, str], tuple[set[int], set[str]]],
        ] = {}
        for item in availability:
            for reference in item.series_refs:
                covered = selected.intersection(reference.covered_units)
                if covered:
                    station_series = grouped.setdefault(
                        (reference.provider, item.station.station_id),
                        {},
                    )
                    key = (
                        reference.series_identity,
                        reference.parameter,
                        reference.channel,
                    )
                    segments, windows = station_series.setdefault(
                        key, (set(), set())
                    )
                    for unit in covered:
                        segments.add(unit.segment_index)
                        if unit.window_index is not None:
                            windows.add(f"window-{unit.window_index:03d}")
        return tuple(
            TopologyStationRef(
                provider=provider,
                station_id=station_id,
                series_refs=tuple(
                    sorted(
                        (
                            TopologySeriesRef(
                                series_identity=series_identity,
                                parameter=parameter,
                                channel=channel,
                                covered_segment_indexes=tuple(sorted(segments)),
                                covered_window_ids=(
                                    tuple(sorted(windows)) if windows else None
                                ),
                            )
                            for (
                                series_identity,
                                parameter,
                                channel,
                            ), (segments, windows) in series_refs.items()
                        ),
                        key=lambda reference: (
                            reference.parameter.code_system,
                            reference.parameter.code,
                            reference.channel,
                            reference.series_identity,
                        ),
                    )
                ),
            )
            for (provider, station_id), series_refs in sorted(
                grouped.items(),
                key=lambda item: (item[0][0].value, item[0][1]),
            )
        )

    @staticmethod
    def _execution_layers(
        nodes: tuple[HydrologyNode, ...],
        active_edges: set[tuple[str, str]],
        *,
        target_comid: str,
    ) -> tuple[ComidLayer, ...]:
        active_nodes = {
            node.comid: node
            for node in nodes
            if node.participation is HydrologyParticipation.ACTIVE
        }
        indegree = {comid: 0 for comid in active_nodes}
        downstreams = {comid: set() for comid in active_nodes}
        for upstream, downstream in active_edges:
            downstreams[upstream].add(downstream)
            indegree[downstream] += 1
        layers: list[ComidLayer] = []
        remaining = set(active_nodes)
        while remaining:
            ready = tuple(
                sorted(
                    (
                        comid
                        for comid in remaining
                        if indegree[comid] == 0
                    ),
                    key=lambda comid: _node_key(active_nodes[comid]),
                )
            )
            if not ready:
                raise TopologyInvariantError("active hydrology graph contains a cycle")
            layers.append(ComidLayer(ready))
            for comid in ready:
                remaining.remove(comid)
                for downstream in downstreams[comid]:
                    indegree[downstream] -= 1
        if layers[-1].root != (target_comid,):
            raise TopologyInvariantError("active hydrology graph does not converge to target")
        return tuple(layers)

    @staticmethod
    def _ensure_deadline(deadline: float, operation: str) -> None:
        if asyncio.get_running_loop().time() >= deadline:
            raise PortTimeoutError(
                "Hydrology topology deadline was exhausted.",
                provider=PROVIDER,
                operation=operation,
            )


def _comid_key(comid: str) -> tuple[int, str]:
    if not comid.isdecimal() or comid.startswith("0"):
        raise TopologyInvariantError("topology COMIDs must be positive decimal strings")
    return int(comid), comid


def _coverage_key(unit: CoverageUnit) -> tuple[int, int, str, str]:
    return (
        unit.segment_index,
        -1 if unit.window_index is None else unit.window_index,
        unit.parameter.code_system,
        unit.parameter.code,
    )


def _state_key(
    state: _PathState,
) -> tuple[object, ...]:
    return (
        state.depth,
        state.cumulative_distance_km,
        *_comid_key(state.comid),
        *_comid_key(state.branch_root_comid),
        state.path_to_target,
        tuple(_coverage_key(unit) for unit in state.coverage_units),
        tuple(_coverage_key(unit) for unit in state.historical_candidates),
    )


def _expansion_key(
    expansion: _Expansion,
) -> tuple[int, float, int, str, int, str, tuple[str, ...]]:
    return (
        expansion.depth,
        expansion.cumulative_distance_km,
        *_comid_key(expansion.segment.comid),
        *_comid_key(expansion.branch_root_comid),
        expansion.parent.path_to_target,
    )


def _node_key(node: HydrologyNode) -> tuple[int, float, int, str]:
    return (
        node.depth,
        node.cumulative_distance_km,
        *_comid_key(node.comid),
    )


def _edge_key(edge: HydrologyEdge) -> tuple[int, str, int, str]:
    return (*_comid_key(edge.upstream_comid), *_comid_key(edge.downstream_comid))


def _outcome_key(
    outcome: BranchOutcome,
) -> tuple[object, ...]:
    return (
        _coverage_key(outcome.coverage_unit),
        *_comid_key(outcome.branch_root_comid),
        len(outcome.path_to_target),
        tuple(_comid_key(comid) for comid in outcome.path_to_target),
        outcome.outcome.value,
        *_optional_comid_key(outcome.blocked_upstream_comid),
        outcome.reason.value,
    )


def _exclusion_key(
    reason: HydrologyExclusionReason,
) -> tuple[object, ...]:
    return (
        reason.code.value,
        *_comid_key(reason.branch_root_comid),
        _coverage_key(reason.coverage_unit),
        *_comid_key(reason.stop_comid),
        *_optional_comid_key(reason.blocked_upstream_comid),
    )


def _optional_comid_key(comid: str | None) -> tuple[int, str]:
    return (-1, "") if comid is None else _comid_key(comid)
