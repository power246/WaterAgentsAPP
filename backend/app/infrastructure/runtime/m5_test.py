from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from itertools import count
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException
from pydantic import field_validator

from app.application.analysis_runs.coordinator import RunCoordinator
from app.application.analysis_runs.models import AgentRole
from app.application.analysis_runs.workflow import CancellationToken, WorkflowContext
from app.application.evidence_tools import (
    EvidenceInspectionPage,
    EvidenceSessionScope,
    EvidenceToolExecution,
    assemble_evidence_session_scope,
)
from app.application.evidence_tools.errors import EvidenceScopeViolationError
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
from app.application.transcripts.read_models import ReadView, SessionTranscriptResponse
from app.application.transcripts.reader import TranscriptReader
from app.application.transcripts.registry import create_production_registry
from app.domain import (
    BranchOutcome,
    BranchOutcomeReason,
    BranchOutcomeStatus,
    CollectionCompletionProof,
    CollectionCounts,
    CollectionPageManifest,
    ComidLayer,
    CoverageUnit,
    DischargeObservationRecord,
    EvidenceLocation,
    EvidenceToolOptions,
    EvidenceToolRequest,
    GeoPoint,
    HydrologyGraph,
    HydrologyNode,
    HydrologyParticipation,
    ModelCapability,
    ModelRole,
    ModelSelection,
    ParameterKey,
    ParameterProvider,
    ProviderCollectionBatch,
    ProviderQuality,
    Provenance,
    ReferenceScope,
    TimeRange,
    TimeResolutionPreference,
    TimeResolutionPreferenceSource,
    TopologyCoverage,
    TopologyLimits,
    TopologySearchStats,
    TopologyStationRef,
    TopologySeriesRef,
    WaterObservationRecord,
    WaterbodyGeometry,
    canonical_model_bytes,
)
from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.infrastructure.config import (
    AppSettings,
    ModelCatalog,
    load_parameter_capability_registry,
)
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from app.infrastructure.runtime.m5 import M5Runtime, compose_m5_runtime
from app.ports import InvalidPortResponseError, PortUnavailableError


TARGET_PARAMETER = ParameterKey(code_system="usgs", code="00300")
SUPPORT_PARAMETER = ParameterKey(code_system="usgs", code="00060")
PARSER_MODEL = ModelSelection(provider="m5-test", model="parser-v1")
WATERBODY_MODEL = ModelSelection(provider="m5-test", model="waterbody-v1")
MODEL_CAPABILITIES = (
    ModelCapability(
        **PARSER_MODEL.model_dump(),
        roles=(ModelRole.QUERY_PARSER,),
    ),
    ModelCapability(
        **WATERBODY_MODEL.model_dump(),
        roles=(ModelRole.WATERBODY,),
    ),
)
FIXTURE_SCOPE_IDS = (
    "station-backed-two-windows",
    "station-backed-empty",
    "station-backed-unavailable",
    "station-backed-invalid",
    "station-backed-cancelled",
    "stationless-two-windows",
)


class M5TestExecuteRequest(DomainModel):
    fixture_scope_id: NonEmptyString
    tool_name: NonEmptyString
    window_ids: tuple[NonEmptyString, ...]
    parameter_keys: tuple[ParameterKey, ...]
    options: EvidenceToolOptions = EvidenceToolOptions()

    @field_validator("window_ids", "parameter_keys")
    @classmethod
    def validate_nonempty_unique_values(
        cls,
        value: tuple[object, ...],
    ) -> tuple[object, ...]:
        if not value:
            raise ValueError("test tool request values must not be empty")
        return ensure_unique_tuple(value, field_name="test tool request values")


@dataclass(frozen=True, slots=True)
class M5TestFixture:
    scope_id: str
    scope: EvidenceSessionScope


@dataclass(slots=True)
class M5TestComposition:
    runtime: M5Runtime
    coordinator: RunCoordinator
    transcript_reader: TranscriptReader
    fixtures: dict[str, M5TestFixture]
    workflow: "_HoldingWorkflow"
    _tool_call_ids: count

    async def execute(
        self,
        payload: M5TestExecuteRequest,
    ) -> tuple[M5TestFixture, EvidenceToolExecution, EvidenceInspectionPage | None]:
        fixture = self.fixtures.get(payload.fixture_scope_id)
        if fixture is None:
            raise KeyError(payload.fixture_scope_id)
        tool_call_id = f"test-call-{next(self._tool_call_ids):06d}"
        execution = await self.runtime.executor.execute_tracked(
            fixture.scope,
            EvidenceToolRequest(
                tool_call_id=tool_call_id,
                session_id=fixture.scope.session_id,
                tool_name=payload.tool_name,
                purpose=f"checked-in fixture {payload.fixture_scope_id}",
                window_ids=payload.window_ids,
                parameter_keys=payload.parameter_keys,
                options=payload.options,
            ),
        )
        return (
            fixture,
            execution,
            self.runtime.executor.inspection_page(
                fixture.scope.run_id,
                tool_call_id,
            ),
        )

    async def read_transcript(self, run_id: UUID) -> SessionTranscriptResponse:
        fixture = next(
            (
                candidate
                for candidate in self.fixtures.values()
                if candidate.scope.run_id == str(run_id)
            ),
            None,
        )
        if fixture is None:
            raise KeyError(str(run_id))
        return await self.transcript_reader.get_session_transcript(
            run_id,
            UUID(fixture.scope.session_id),
            ReadView.AUTO,
        )


class _StepClock:
    def __init__(self) -> None:
        self._value = datetime(2026, 10, 1, tzinfo=UTC)

    def now(self) -> datetime:
        value = self._value
        self._value += timedelta(microseconds=1)
        return value


class _HoldingWorkflow:
    def __init__(self) -> None:
        self.contexts: asyncio.Queue[WorkflowContext] = asyncio.Queue()
        self.release = asyncio.Event()

    async def run(self, context: WorkflowContext) -> None:
        self.contexts.put_nowait(context)
        await self.release.wait()
        context.cancellation.checkpoint()


class _FixtureWaterPort:
    def __init__(
        self,
        *,
        scopes_by_station: dict[str, EvidenceSessionScope],
        scenarios_by_station: dict[str, str],
        clock: _StepClock,
    ) -> None:
        self._scopes_by_station = scopes_by_station
        self._scenarios_by_station = scenarios_by_station
        self._clock = clock

    async def collect(self, request, *, deadline=None):
        del deadline
        scenario = self._scenarios_by_station[request.station_id]
        if scenario == "unavailable":
            raise PortUnavailableError(
                "fixture provider unavailable",
                provider="m5_fixture",
                operation=request.operation_id,
            )
        if scenario == "invalid":
            raise InvalidPortResponseError(
                "fixture provider response invalid",
                provider="m5_fixture",
                operation=request.operation_id,
            )
        scope = self._scopes_by_station[request.station_id]
        records = ()
        if scenario == "happy":
            records = tuple(
                _observation_record(
                    request,
                    scope,
                    window_id,
                    index,
                    retrieved_at=self._clock.now(),
                )
                for index, window_id in enumerate(
                    request.affected_window_ids,
                    start=1,
                )
            )
        return _completed_batch(
            request.operation_id,
            records,
            completed_ranges=(request.request_range,),
        )


class _EmptyWeatherPort:
    async def collect(self, request, *, deadline=None):
        del deadline
        return _completed_batch(
            request.operation_id,
            (),
            completed_ranges=(request.request_range,),
        )


class _EmptyFacilityPort:
    async def collect(self, request, *, deadline=None):
        del deadline
        return _completed_batch(request.operation_id, (), completed_ranges=())


def create_m5_test_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        composition = await _create_test_composition()
        application.state.m5_test_composition = composition
        try:
            yield
        finally:
            composition.workflow.release.set()
            await composition.coordinator.shutdown()

    application = FastAPI(
        title="WaterAgents M5 contract test app",
        lifespan=lifespan,
        redirect_slashes=False,
    )

    @application.post("/__test__/m5/evidence-tools:execute")
    async def execute_evidence_tool(payload: M5TestExecuteRequest):
        composition: M5TestComposition = application.state.m5_test_composition
        try:
            fixture, execution, inspection = await composition.execute(payload)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="unknown fixture scope") from error
        except EvidenceScopeViolationError as error:
            raise HTTPException(
                status_code=422,
                detail="evidence request failed preflight authorization",
            ) from error
        return {
            "fixture_scope_id": fixture.scope_id,
            "run_id": fixture.scope.run_id,
            "session_id": fixture.scope.session_id,
            "result": execution.result.model_dump(mode="json"),
            "result_ref": execution.result_ref.model_dump(mode="json"),
            "inspection": (
                {
                    "source_item_id": inspection.source_item_id,
                    "offset": inspection.offset,
                    "limit": inspection.limit,
                    "total_records": inspection.total_records,
                    "records": [
                        record.model_dump(mode="json")
                        for record in inspection.records
                    ],
                    "record_refs": [
                        reference.model_dump(mode="json")
                        for reference in inspection.record_refs
                    ],
                    "next_offset": inspection.next_offset,
                }
                if inspection is not None
                else None
            ),
        }

    @application.get(
        "/__test__/m5/runs/{run_id}/transcript",
        response_model=SessionTranscriptResponse,
    )
    async def read_test_transcript(run_id: UUID) -> SessionTranscriptResponse:
        composition: M5TestComposition = application.state.m5_test_composition
        try:
            return await composition.read_transcript(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="unknown fixture run") from error

    return application


async def _create_test_composition() -> M5TestComposition:
    settings = AppSettings(
        _env_file=None,
        model_catalog=MODEL_CAPABILITIES,
        transcript_inline_max_bytes=512,
        transcript_preview_bytes=128,
    )
    clock = _StepClock()
    workflow = _HoldingWorkflow()
    store = InMemoryTranscriptStore()
    transcript_registry = create_production_registry()
    capabilities = load_parameter_capability_registry()
    coordinator = RunCoordinator(
        clock,
        transcript_store=store,
        model_catalog=ModelCatalog(MODEL_CAPABILITIES),
        analysis_timezone=settings.analysis_timezone,
        workflow=workflow,
        shutdown_grace_period=0,
        shutdown_save_grace_period=0,
        inline_content_bytes=settings.transcript_inline_max_bytes,
        preview_bytes=settings.transcript_preview_bytes // 2,
        live_state_grace_period=0,
        transcript_registry=transcript_registry,
    )
    reader = TranscriptReader(
        coordinator,
        store,
        transcript_registry,
        error_id_factory=uuid4,
        inline_content_bytes=settings.transcript_inline_max_bytes,
        preview_bytes=settings.transcript_preview_bytes // 2,
    )
    fixture_data: dict[str, M5TestFixture] = {}
    scopes_by_station: dict[str, EvidenceSessionScope] = {}
    scenarios_by_station: dict[str, str] = {}
    scenario_by_fixture = {
        "station-backed-two-windows": "happy",
        "station-backed-empty": "empty",
        "station-backed-unavailable": "unavailable",
        "station-backed-invalid": "invalid",
        "station-backed-cancelled": "cancelled",
        "stationless-two-windows": "stationless",
    }
    for fixture_scope_id in FIXTURE_SCOPE_IDS:
        scenario = scenario_by_fixture[fixture_scope_id]
        station_id = f"USGS-M5-{scenario.upper()}"
        fixture = await _create_fixture_scope(
            fixture_scope_id,
            station_id=station_id,
            cancelled=scenario == "cancelled",
            coordinator=coordinator,
            workflow=workflow,
            registry=capabilities,
        )
        fixture_data[fixture_scope_id] = fixture
        scopes_by_station[station_id] = fixture.scope
        scenarios_by_station[station_id] = scenario

    water_port = _FixtureWaterPort(
        scopes_by_station=scopes_by_station,
        scenarios_by_station=scenarios_by_station,
        clock=clock,
    )
    runtime = compose_m5_runtime(
        settings,
        operations=coordinator,
        transcript_reader=reader,
        capability_registry=capabilities,
        water_port=water_port,
        weather_port=_EmptyWeatherPort(),
        facility_port=_EmptyFacilityPort(),
        clock=clock,
        operation_id_factory=_sequential_uuid_ids(),
    )
    return M5TestComposition(
        runtime=runtime,
        coordinator=coordinator,
        transcript_reader=reader,
        fixtures=fixture_data,
        workflow=workflow,
        _tool_call_ids=count(1),
    )


async def _create_fixture_scope(
    fixture_scope_id: str,
    *,
    station_id: str,
    cancelled: bool,
    coordinator: RunCoordinator,
    workflow: _HoldingWorkflow,
    registry,
) -> M5TestFixture:
    created = await coordinator.create_run(_create_request_bytes(fixture_scope_id))
    await workflow.contexts.get()
    parser = await coordinator.prepare_agent_session(
        created.run_id,
        agent_role=AgentRole.QUERY_PARSER,
    )
    await coordinator.mark_agent_session_running(created.run_id, parser.session_id)
    context = _analysis_context()
    context_item = await coordinator.reserve_transcript_item(
        created.run_id,
        session_id=parser.session_id,
    )
    await coordinator.finalize_transcript_item(
        created.run_id,
        context_item.item_id,
        "query_understood",
        1,
        context,
    )
    graph = _hydrology_graph(
        station_id,
        with_station=not fixture_scope_id.startswith("stationless-"),
    )
    topology_item = await coordinator.reserve_transcript_item(created.run_id)
    await coordinator.finalize_transcript_item(
        created.run_id,
        topology_item.item_id,
        "topology_ready",
        1,
        graph,
    )
    session = await coordinator.prepare_agent_session(
        created.run_id,
        agent_role=AgentRole.WATERBODY,
        target_comid="123",
    )
    await coordinator.mark_agent_session_running(created.run_id, session.session_id)
    cancel_event = asyncio.Event()
    if cancelled:
        cancel_event.set()
    scope = assemble_evidence_session_scope(
        run_id=str(created.run_id),
        session_id=str(session.session_id),
        session_fact=ReferenceScope(
            item_id=str(session.session_id),
            run_id=str(created.run_id),
            session_id=str(session.session_id),
        ),
        analysis_context_ref=ReferenceScope(
            item_id=str(context_item.item_id),
            run_id=str(created.run_id),
        ),
        topology_ref=ReferenceScope(
            item_id=str(topology_item.item_id),
            run_id=str(created.run_id),
        ),
        context=context,
        topology=graph,
        agent_comid="123",
        registry=registry,
        cancel_token=CancellationToken(cancel_event),
    )
    return M5TestFixture(scope_id=fixture_scope_id, scope=scope)


def _analysis_context() -> AnalysisContext:
    registry = load_parameter_capability_registry()
    target_capability = registry.get(TARGET_PARAMETER)
    support_capability = registry.get(SUPPORT_PARAMETER)
    if target_capability is None or support_capability is None:
        raise RuntimeError("checked-in M5 fixture parameters are unavailable")
    decision = datetime(2026, 9, 28, 12, tzinfo=UTC)
    parsed = ParsedIntentV1(
        start_date=date(2026, 9, 29),
        end_date=date(2026, 9, 30),
        parameters=(
            ParameterConcept(
                concept="dissolved oxygen",
                rationale="Checked-in M5 contract fixture.",
                explicitly_named=True,
            ),
        ),
    )
    resolved = DateResolver().resolve(
        None,
        parsed,
        decision_instant=decision,
        analysis_timezone="America/Chicago",
        resolution_preference=TimeResolutionPreference.TWENTY_FOUR_HOURS,
        resolution_preference_source=TimeResolutionPreferenceSource.FRONTEND,
    )
    if resolved.temporal is None:
        raise RuntimeError("checked-in M5 fixture dates did not resolve")
    temporal = resolved.temporal
    return AnalysisContext(
        query="Analyze dissolved oxygen for the checked-in M5 fixture.",
        decision_instant=decision,
        analysis_timezone="America/Chicago",
        target=ResolvedTarget(
            comid="123",
            point=GeoPoint(latitude=39.0, longitude=-77.0),
            display_name="M5 fixture reach",
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
                key=target_capability.key,
                canonical_name=target_capability.canonical_name,
                canonical_unit=target_capability.canonical_unit,
                source=ParameterIntentSource.EXPLICIT,
                rationale="Checked-in M5 contract fixture.",
            ),
        ),
        supporting_params=(
            SupportingParameterIntent(
                key=support_capability.key,
                canonical_name=support_capability.canonical_name,
                canonical_unit=support_capability.canonical_unit,
                reason=SupportingIntentReason.CAPABILITY_REQUIRED,
            ),
        ),
        parser_model=PARSER_MODEL,
        waterbody_model=WATERBODY_MODEL,
        memory_mode="stateless",
    )


def _hydrology_graph(
    station_id: str,
    *,
    with_station: bool = True,
) -> HydrologyGraph:
    geometry = WaterbodyGeometry.from_coordinates(
        source_comid="123",
        coordinates=((-77.01, 39.0), (-76.99, 39.0)),
    )
    unit = CoverageUnit(parameter=SUPPORT_PARAMETER, segment_index=0)
    station = TopologyStationRef(
        provider=ParameterProvider.WDFN_TIME_SERIES,
        station_id=station_id,
        series_refs=(
            TopologySeriesRef(
                series_identity=f"series-{station_id.casefold()}",
                parameter=SUPPORT_PARAMETER,
                channel="continuous",
                covered_segment_indexes=(0,),
            ),
        ),
    )
    node = HydrologyNode(
        comid="123",
        point=geometry.representative_point,
        geometry_ref=geometry.geometry_ref,
        geometry_summary=geometry.summary(),
        depth=0,
        cumulative_distance_km=0,
        is_target=True,
        is_inlet=with_station,
        participation=HydrologyParticipation.ACTIVE,
        qualified_units=((unit,) if with_station else ()),
        station_refs=((station,) if with_station else ()),
    )
    return HydrologyGraph(
        target_comid="123",
        coverage=(
            TopologyCoverage.COMPLETE if with_station else TopologyCoverage.NONE
        ),
        nodes=(node,),
        geometries={geometry.geometry_ref: geometry},
        edges=(),
        branch_outcomes=(
            BranchOutcome(
                branch_root_comid="123",
                coverage_unit=unit,
                outcome=(
                    BranchOutcomeStatus.QUALIFIED
                    if with_station
                    else BranchOutcomeStatus.NO_QUALIFYING_SOURCE
                ),
                stop_comid="123",
                path_to_target=("123",),
                reason=(
                    BranchOutcomeReason.TARGET_STATION_QUALIFIED
                    if with_station
                    else BranchOutcomeReason.NATURAL_HEADWATER
                ),
            ),
        ),
        execution_layers=(ComidLayer(("123",)),),
        limits=TopologyLimits(max_distance_km=100, max_hops=12, max_nodes=128),
        stats=TopologySearchStats(
            discovered_nodes=1,
            queried_nodes=1,
            max_depth=0,
            max_distance_km=0,
            qualified_inlets=int(with_station),
            excluded_nodes=0,
        ),
    )


def _observation_record(
    request,
    scope: EvidenceSessionScope,
    window_id: str,
    index: int,
    *,
    retrieved_at: datetime,
):
    window = next(item for item in scope.time_windows if item.window_id == window_id)
    parameter = request.parameter_keys[0]
    discharge = parameter == SUPPORT_PARAMETER
    record_type = DischargeObservationRecord if discharge else WaterObservationRecord
    page_identity = f"page:{request.operation_id}"
    daily = request.channel == "daily"
    return record_type(
        provider="usgs_waterdata",
        channel=request.channel,
        source_uri="https://api.waterdata.usgs.gov/ogcapi/collections/continuous/items",
        retrieved_at=retrieved_at,
        observed_time=(
            None
            if daily
            else window.time_range.start + timedelta(minutes=index)
        ),
        valid_time_range=window.time_range if daily else None,
        location=EvidenceLocation(
            point=None,
            comid=None,
            station_id=request.station_id,
            distance_to_target_km=0,
        ),
        provider_quality=ProviderQuality(approval="approved"),
        raw_unit="ft3/s" if discharge else "mg/L",
        provenance=Provenance(
            operation_id=request.operation_id,
            request_identity=f"request:{request.operation_id}",
            page_identity=page_identity,
            provider_series_id=request.series_refs[0],
            provider_record_id=(
                f"fixture-{request.channel}-{parameter.code}-{window_id}"
            ),
        ),
        parameter=parameter,
        value=str(7 + index),
        medium="Water",
        fraction=None if discharge else "Dissolved",
        method="provider_statistic" if daily else "sensor",
        statistic="mean" if daily else "instantaneous",
    )


def _completed_batch(operation_id, records, *, completed_ranges):
    records = tuple(records)
    response_bytes = max(
        1,
        sum(len(canonical_model_bytes(record)) for record in records),
    )
    page_identity = f"page:{operation_id}"
    return ProviderCollectionBatch(
        operation_id=operation_id,
        records=records,
        page_or_chunk_manifests=(
            CollectionPageManifest(
                page_identity=page_identity,
                record_count=len(records),
                response_bytes=response_bytes,
                page_complete=True,
            ),
        ),
        completed_ranges=tuple(completed_ranges),
        counts=CollectionCounts(
            requests=1,
            pages=1,
            records=len(records),
            response_bytes=response_bytes,
        ),
        completion_proof=CollectionCompletionProof(
            contract="single_response",
            complete=True,
            terminal_identity=page_identity,
            digest=f"fixture:{operation_id}",
        ),
    )


def _create_request_bytes(query: str) -> bytes:
    return json.dumps(
        {
            "query": query,
            "parser_model": PARSER_MODEL.model_dump(mode="json"),
            "waterbody_model": WATERBODY_MODEL.model_dump(mode="json"),
            "memory_mode": "stateless",
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _sequential_uuid_ids():
    values = count(1)
    return lambda: str(UUID(int=next(values)))
