import asyncio
from datetime import date, datetime, time, timezone

import pytest

from app.application.analysis_runs.workflow import CancellationToken
from app.application.evidence_tools import assemble_evidence_session_scope
from app.application.query_understanding import (
    AnalysisContext,
    DateResolver,
    ParameterConcept,
    ParameterIntentSource,
    ParsedIntentV1,
    ResolvedParameterIntent,
    ResolvedTarget,
    SupportingIntentReason,
    SupportingParameterIntent,
    TargetSource,
)
from app.domain import (
    BranchOutcome,
    BranchOutcomeReason,
    BranchOutcomeStatus,
    ComidLayer,
    CoverageUnit,
    GeoPoint,
    HydrologyGraph,
    HydrologyNode,
    HydrologyParticipation,
    MemoryMode,
    ModelSelection,
    ParameterKey,
    ParameterProvider,
    ReferenceScope,
    TimeResolutionPreference,
    TimeResolutionPreferenceSource,
    TopologyCoverage,
    TopologyLimits,
    TopologySearchStats,
    TopologyStationRef,
    WaterbodyGeometry,
)
from app.infrastructure.config import load_parameter_capability_registry


TARGET_PARAMETER = ParameterKey(code_system="usgs", code="00300")
SUPPORT_PARAMETER = ParameterKey(code_system="usgs", code="00060")
MODEL = ModelSelection(provider="openai", model="test-model")


def make_context(
    *,
    start_date: date = date(2026, 9, 29),
    end_date: date = date(2026, 9, 29),
    analysis_timezone: str = "America/Chicago",
    decision_instant: datetime | None = None,
    memory_mode: MemoryMode = MemoryMode.STATELESS,
) -> AnalysisContext:
    registry = load_parameter_capability_registry()
    target = registry.get(TARGET_PARAMETER)
    support = registry.get(SUPPORT_PARAMETER)
    assert target is not None and support is not None
    decision = decision_instant or datetime.combine(
        start_date,
        time(hour=12),
        timezone.utc,
    )
    parsed = ParsedIntentV1(
        start_date=start_date,
        end_date=end_date,
        parameters=(
            ParameterConcept(
                concept="dissolved oxygen",
                rationale="Requested test parameter.",
                explicitly_named=True,
            ),
        ),
    )
    resolved = DateResolver().resolve(
        None,
        parsed,
        decision_instant=decision,
        analysis_timezone=analysis_timezone,
        resolution_preference=TimeResolutionPreference.AUTO,
        resolution_preference_source=TimeResolutionPreferenceSource.FRONTEND,
    )
    assert resolved.temporal is not None
    temporal = resolved.temporal
    return AnalysisContext(
        query="Analyze dissolved oxygen.",
        decision_instant=decision,
        analysis_timezone=analysis_timezone,
        target=ResolvedTarget(
            comid="123",
            point=GeoPoint(latitude=39.0, longitude=-77.0),
            display_name="Test reach",
            country_code="US",
            subdivision_code="US-DC",
            source=TargetSource.QUERY,
        ),
        requested_date_range=temporal.requested_date_range,
        time_range=temporal.time_range,
        time_segments=temporal.time_segments,
        mode=temporal.mode,
        time_resolution=temporal.time_resolution,
        time_resolution_source=temporal.time_resolution_source,
        params_used=(
            ResolvedParameterIntent(
                key=target.key,
                canonical_name=target.canonical_name,
                canonical_unit=target.canonical_unit,
                source=ParameterIntentSource.EXPLICIT,
                rationale="Requested test parameter.",
            ),
        ),
        supporting_params=(
            SupportingParameterIntent(
                key=support.key,
                canonical_name=support.canonical_name,
                canonical_unit=support.canonical_unit,
                reason=SupportingIntentReason.CAPABILITY_REQUIRED,
            ),
        ),
        parser_model=MODEL,
        waterbody_model=MODEL,
        memory_mode=memory_mode,
    )


def make_graph(*, with_geometry: bool = True) -> HydrologyGraph:
    geometry = (
        WaterbodyGeometry.from_coordinates(
            source_comid="123",
            coordinates=((-77.01, 39.0), (-76.99, 39.0)),
        )
        if with_geometry
        else None
    )
    point = (
        geometry.representative_point
        if geometry is not None
        else GeoPoint(latitude=39.0, longitude=-77.0)
    )
    unit = CoverageUnit(parameter=SUPPORT_PARAMETER, segment_index=0)
    node = HydrologyNode(
        comid="123",
        point=point,
        geometry_ref=geometry.geometry_ref if geometry is not None else None,
        geometry_summary=geometry.summary() if geometry is not None else None,
        depth=0,
        cumulative_distance_km=0,
        is_target=True,
        is_inlet=True,
        participation=HydrologyParticipation.ACTIVE,
        qualified_units=(unit,),
        station_refs=(
            TopologyStationRef(
                provider=ParameterProvider.WDFN_TIME_SERIES,
                station_id="USGS-123",
                series_refs=("series-123",),
            ),
        ),
    )
    return HydrologyGraph(
        target_comid="123",
        coverage=TopologyCoverage.COMPLETE,
        nodes=(node,),
        geometries=({geometry.geometry_ref: geometry} if geometry is not None else {}),
        edges=(),
        branch_outcomes=(
            BranchOutcome(
                branch_root_comid="123",
                coverage_unit=unit,
                outcome=BranchOutcomeStatus.QUALIFIED,
                stop_comid="123",
                path_to_target=("123",),
                reason=BranchOutcomeReason.TARGET_STATION_QUALIFIED,
            ),
        ),
        execution_layers=(ComidLayer(("123",)),),
        limits=TopologyLimits(max_distance_km=100, max_hops=12, max_nodes=128),
        stats=TopologySearchStats(
            discovered_nodes=1,
            queried_nodes=1,
            max_depth=0,
            max_distance_km=0,
            qualified_inlets=1,
            excluded_nodes=0,
        ),
    )


def make_scope(*, context: AnalysisContext | None = None, with_geometry: bool = True):
    return assemble_evidence_session_scope(
        run_id="run-1",
        session_id="session-1",
        session_fact=ReferenceScope(
            item_id="session-fact",
            run_id="run-1",
            session_id="session-1",
        ),
        analysis_context_ref=ReferenceScope(
            item_id="context-item",
            run_id="run-1",
        ),
        topology_ref=ReferenceScope(
            item_id="topology-item",
            run_id="run-1",
        ),
        context=context or make_context(),
        topology=make_graph(with_geometry=with_geometry),
        agent_comid="123",
        registry=load_parameter_capability_registry(),
        cancel_token=CancellationToken(asyncio.Event()),
    )


@pytest.fixture
def evidence_scope():
    return make_scope()


@pytest.fixture
def context_factory():
    return make_context


@pytest.fixture
def graph_factory():
    return make_graph


@pytest.fixture
def scope_factory():
    return make_scope
