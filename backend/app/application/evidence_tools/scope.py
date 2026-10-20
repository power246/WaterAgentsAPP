from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from app.application.analysis_runs.workflow import CancellationToken
from app.application.query_understanding import AnalysisContext, EvidenceRulePlan
from app.domain import (
    GeoPoint,
    HydrologyGraph,
    HydrologyNode,
    HydrologyParticipation,
    InferenceMode,
    MemoryMode,
    ParameterCapabilityRegistry,
    ParameterKey,
    ReferenceScope,
    TimeRange,
    TimeResolution,
    TimeResolutionSource,
    TopologyStationRef,
    build_time_windows,
)
from app.domain.time.timezone import get_iana_timezone

_SCOPE_SEAL = object()


@dataclass(frozen=True, slots=True)
class EvidenceWindow:
    window_id: str
    time_range: TimeRange
    segment_index: int
    window_kind: InferenceMode | None = None


@dataclass(frozen=True, slots=True, init=False)
class EvidenceSessionScope:
    run_id: str
    session_id: str
    session_ref: ReferenceScope
    agent_comid: str
    query: str
    analysis_context_ref: ReferenceScope
    topology_ref: ReferenceScope
    topology: HydrologyGraph
    target_node: HydrologyNode
    analysis_target_point: GeoPoint
    time_windows: tuple[EvidenceWindow, ...]
    requested_parameters: tuple[ParameterKey, ...]
    supporting_parameters: tuple[ParameterKey, ...]
    intermediate_parameters: tuple[ParameterKey, ...]
    evidence_rule_plan: EvidenceRulePlan
    station_refs: tuple[TopologyStationRef, ...]
    geometry_ref: str | None
    direct_upstream_comids: tuple[str, ...]
    memory_mode: MemoryMode
    mode: InferenceMode
    analysis_timezone: str
    time_resolution: TimeResolution
    time_resolution_source: TimeResolutionSource
    query_envelope: TimeRange
    cancel_token: CancellationToken

    def __init__(
        self,
        *,
        run_id: str,
        session_id: str,
        session_ref: ReferenceScope,
        agent_comid: str,
        query: str,
        analysis_context_ref: ReferenceScope,
        topology_ref: ReferenceScope,
        topology: HydrologyGraph,
        target_node: HydrologyNode,
        analysis_target_point: GeoPoint,
        time_windows: tuple[EvidenceWindow, ...],
        requested_parameters: tuple[ParameterKey, ...],
        supporting_parameters: tuple[ParameterKey, ...],
        evidence_rule_plan: EvidenceRulePlan,
        station_refs: tuple[TopologyStationRef, ...],
        geometry_ref: str | None,
        direct_upstream_comids: tuple[str, ...],
        memory_mode: MemoryMode,
        mode: InferenceMode,
        analysis_timezone: str,
        time_resolution: TimeResolution,
        time_resolution_source: TimeResolutionSource,
        query_envelope: TimeRange,
        cancel_token: CancellationToken,
        _seal: object,
        intermediate_parameters: tuple[ParameterKey, ...] = (),
    ) -> None:
        if _seal is not _SCOPE_SEAL:
            raise TypeError("EvidenceSessionScope must be assembled from saved facts")
        for name, value in (("run_id", run_id), ("session_id", session_id)):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        if len(intermediate_parameters) != len(set(intermediate_parameters)):
            raise ValueError("intermediate parameters must be unique")
        if set(intermediate_parameters) & (
            set(requested_parameters) | set(supporting_parameters)
        ):
            raise ValueError("intermediate parameters must not overlap target or support")
        values = {
            "run_id": run_id,
            "session_id": session_id,
            "session_ref": session_ref,
            "agent_comid": agent_comid,
            "query": query,
            "analysis_context_ref": analysis_context_ref,
            "topology_ref": topology_ref,
            "topology": topology,
            "target_node": target_node,
            "analysis_target_point": analysis_target_point,
            "time_windows": time_windows,
            "requested_parameters": requested_parameters,
            "supporting_parameters": supporting_parameters,
            "intermediate_parameters": intermediate_parameters,
            "evidence_rule_plan": evidence_rule_plan,
            "station_refs": station_refs,
            "geometry_ref": geometry_ref,
            "direct_upstream_comids": direct_upstream_comids,
            "memory_mode": memory_mode,
            "mode": mode,
            "analysis_timezone": analysis_timezone,
            "time_resolution": time_resolution,
            "time_resolution_source": time_resolution_source,
            "query_envelope": query_envelope,
            "cancel_token": cancel_token,
        }
        for field_name, field_value in values.items():
            object.__setattr__(self, field_name, field_value)


def assemble_evidence_session_scope(
    *,
    run_id: str,
    session_id: str,
    session_fact: ReferenceScope,
    analysis_context_ref: ReferenceScope,
    topology_ref: ReferenceScope,
    context: AnalysisContext,
    topology: HydrologyGraph,
    agent_comid: str,
    registry: ParameterCapabilityRegistry,
    cancel_token: CancellationToken,
) -> EvidenceSessionScope:
    """Build a sealed scope only after saved run-global facts agree."""

    if session_fact.run_id != run_id or session_fact.session_id != session_id:
        raise ValueError("session fact must match the requested run and session")
    for label, reference in (
        ("analysis context", analysis_context_ref),
        ("topology", topology_ref),
    ):
        if reference.run_id != run_id or reference.session_id is not None:
            raise ValueError(f"{label} must be a run-global fact from this run")
    if context.target.comid != topology.target_comid:
        raise ValueError("saved context and topology target must match")
    context.validate_capability_registry(registry)
    node = next((item for item in topology.nodes if item.comid == agent_comid), None)
    if node is None or node.participation is not HydrologyParticipation.ACTIVE:
        raise ValueError("agent COMID must be an active node in saved topology")
    if node.geometry_ref is not None and node.geometry_ref not in topology.geometries:
        raise ValueError("agent geometry reference must resolve in saved topology")

    windows = tuple(
        EvidenceWindow(
            window_id=f"window-{index:03d}",
            time_range=value,
            segment_index=_segment_index(context, value),
            window_kind=context.time_segments[_segment_index(context, value)].mode,
        )
        for index, value in enumerate(
            build_time_windows(
                context.time_range,
                context.time_resolution,
                context.analysis_timezone,
            )
        )
    )
    if not windows:
        raise ValueError("saved context must produce at least one result window")
    direct_upstreams = tuple(
        sorted(
            edge.upstream_comid
            for edge in topology.edges
            if edge.downstream_comid == agent_comid and edge.participating
        )
    )
    return EvidenceSessionScope(
        run_id=run_id,
        session_id=session_id,
        session_ref=session_fact,
        agent_comid=agent_comid,
        query=context.query,
        analysis_context_ref=analysis_context_ref,
        topology_ref=topology_ref,
        topology=topology,
        target_node=node,
        analysis_target_point=context.target.point,
        time_windows=windows,
        requested_parameters=tuple(item.key for item in context.params_used),
        supporting_parameters=tuple(item.key for item in context.supporting_params),
        evidence_rule_plan=context.evidence_rule_plan,
        station_refs=node.station_refs,
        geometry_ref=node.geometry_ref,
        direct_upstream_comids=direct_upstreams,
        memory_mode=context.memory_mode,
        mode=context.mode,
        analysis_timezone=context.analysis_timezone,
        time_resolution=context.time_resolution,
        time_resolution_source=context.time_resolution_source,
        query_envelope=_build_query_envelope(context),
        cancel_token=cancel_token,
        _seal=_SCOPE_SEAL,
    )


def with_evidence_rule_plan(
    scope: EvidenceSessionScope,
    evidence_rule_plan: EvidenceRulePlan,
) -> EvidenceSessionScope:
    """Return the same sealed session scope with an append-only rule-plan view."""

    return _clone_scope(scope, evidence_rule_plan, scope.intermediate_parameters)


def with_intermediate_parameters(
    scope: EvidenceSessionScope,
    parameters: tuple[ParameterKey, ...],
) -> EvidenceSessionScope:
    """Set plan-derived parameters without changing requested or required support."""

    return _clone_scope(scope, scope.evidence_rule_plan, parameters)


def _clone_scope(
    scope: EvidenceSessionScope,
    evidence_rule_plan: EvidenceRulePlan,
    intermediate_parameters: tuple[ParameterKey, ...],
) -> EvidenceSessionScope:
    return EvidenceSessionScope(
        run_id=scope.run_id,
        session_id=scope.session_id,
        session_ref=scope.session_ref,
        agent_comid=scope.agent_comid,
        query=scope.query,
        analysis_context_ref=scope.analysis_context_ref,
        topology_ref=scope.topology_ref,
        topology=scope.topology,
        target_node=scope.target_node,
        analysis_target_point=scope.analysis_target_point,
        time_windows=scope.time_windows,
        requested_parameters=scope.requested_parameters,
        supporting_parameters=scope.supporting_parameters,
        intermediate_parameters=intermediate_parameters,
        evidence_rule_plan=evidence_rule_plan,
        station_refs=scope.station_refs,
        geometry_ref=scope.geometry_ref,
        direct_upstream_comids=scope.direct_upstream_comids,
        memory_mode=scope.memory_mode,
        mode=scope.mode,
        analysis_timezone=scope.analysis_timezone,
        time_resolution=scope.time_resolution,
        time_resolution_source=scope.time_resolution_source,
        query_envelope=scope.query_envelope,
        cancel_token=scope.cancel_token,
        _seal=_SCOPE_SEAL,
    )


def _segment_index(context: AnalysisContext, window: TimeRange) -> int:
    for segment in context.time_segments:
        if (
            window.start >= segment.time_range.start
            and window.end <= segment.time_range.end
        ):
            return segment.segment_index
    raise ValueError("result window must belong to one analysis segment")


def _build_query_envelope(context: AnalysisContext) -> TimeRange:
    zone = get_iana_timezone(context.analysis_timezone)
    requested = context.requested_date_range
    local_start = datetime.combine(
        requested.start_date - timedelta(days=1),
        time.min,
        zone,
    )
    local_end = datetime.combine(
        requested.end_date + timedelta(days=2),
        time.min,
        zone,
    )
    return TimeRange(
        start=local_start.astimezone(timezone.utc),
        end=local_end.astimezone(timezone.utc),
    )
