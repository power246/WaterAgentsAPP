import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.application.analysis_runs.models import (
    AgentRole,
    AgentSessionStatus,
    AnalysisPhase,
    RunStatus,
)
from app.application.hydrology_topology import (
    HydrologyGraphBuilder,
    TopologyInvariantError,
)
from app.application.query_understanding import (
    AnalysisContext,
    AnalysisContextResolution,
    AnalysisContextResolutionStatus,
    DateResolver,
    ParameterIntentSource,
    ParsedIntentV1,
    ParserFinishReason,
    QueryParserResponse,
    ResolvedParameterIntent,
    ResolvedTarget,
    SupportingIntentReason,
    SupportingParameterIntent,
    TargetSource,
)
from app.application.transcripts.registry import create_production_registry
from app.domain import (
    AvailabilityCoverageSource,
    GeoPoint,
    MonitoringStation,
    ParameterKey,
    ParameterProvider,
    RiverSegment,
    SeriesRef,
    StationAvailability,
    TopologyLimits,
)
from app.infrastructure.config import AppSettings, load_parameter_capability_registry
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from app.infrastructure.runtime import compose_m4_workflow
from app.ports import InvalidPortResponseError, PortUnavailableError
from tests.m4_support import create_request_bytes, create_test_app

UTC = timezone.utc
NOW = datetime(2026, 9, 25, 8, 0, tzinfo=UTC)
TARGET = "100"
UPSTREAM = "200"


class FixedClock:
    def now(self) -> datetime:
        return NOW


def parser_output() -> str:
    return json.dumps(
        {
            "location_mention": None,
            "coordinate_mention": {"latitude": 38.9, "longitude": -77.0},
            "start_date": "2026-09-24",
            "end_date": "2026-09-24",
            "parameters": [
                {
                    "concept": "dissolved oxygen",
                    "rationale": "Dissolved oxygen is explicitly requested.",
                    "explicitly_named": True,
                }
            ],
            "ambiguities": [],
        },
        separators=(",", ":"),
    )


class RecordingParser:
    def __init__(self) -> None:
        self.calls = []

    async def generate(self, request, on_delta):
        self.calls.append(request)
        text = parser_output()
        await on_delta(text[: len(text) // 2])
        await on_delta(text[len(text) // 2 :])
        return QueryParserResponse(text=text, finish_reason=ParserFinishReason.STOP)


class RepairBlockingParser:
    def __init__(self) -> None:
        self.calls = []
        self.repair_started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def generate(self, request, on_delta):
        self.calls.append(request)
        if len(self.calls) == 1:
            invalid = '{"parameters":[]}'
            await on_delta(invalid)
            return QueryParserResponse(
                text=invalid,
                finish_reason=ParserFinishReason.STOP,
            )
        self.repair_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class RecordingResolver:
    def __init__(
        self,
        status: AnalysisContextResolutionStatus = (
            AnalysisContextResolutionStatus.SUCCESS
        ),
    ) -> None:
        self.status = status
        self.calls: list[ParsedIntentV1] = []

    async def resolve(
        self,
        request,
        parsed_intent,
        *,
        analysis_timezone,
        decision_instant,
    ) -> AnalysisContextResolution:
        self.calls.append(parsed_intent)
        if self.status is not AnalysisContextResolutionStatus.SUCCESS:
            return AnalysisContextResolution(status=self.status)
        configuration = request.to_configuration_snapshot(analysis_timezone)
        dates = DateResolver().resolve(
            request.date_range,
            parsed_intent,
            decision_instant=decision_instant,
            analysis_timezone=analysis_timezone,
            resolution_preference=configuration.time_resolution_preference,
            resolution_preference_source=(
                configuration.time_resolution_preference_source
            ),
        )
        assert dates.temporal is not None
        temporal = dates.temporal
        context = AnalysisContext(
            query=request.query,
            decision_instant=decision_instant,
            analysis_timezone=analysis_timezone,
            target=ResolvedTarget(
                comid=TARGET,
                point=GeoPoint(latitude=38.9, longitude=-77.0),
                display_name="Target reach",
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
                    key=ParameterKey(code_system="usgs", code="00300"),
                    canonical_name="Dissolved oxygen",
                    canonical_unit="mg/l",
                    source=ParameterIntentSource.EXPLICIT,
                    rationale=parsed_intent.parameters[0].rationale,
                ),
            ),
            supporting_params=(
                SupportingParameterIntent(
                    key=ParameterKey(code_system="usgs", code="00060"),
                    canonical_name="Discharge",
                    canonical_unit="ft3/s",
                    reason=SupportingIntentReason.CAPABILITY_REQUIRED,
                ),
            ),
            parser_model=request.parser_model,
            waterbody_model=request.waterbody_model,
            memory_mode=request.memory_mode,
        )
        context.validate_configuration(configuration)
        return AnalysisContextResolution(
            status=AnalysisContextResolutionStatus.SUCCESS,
            context=context,
        )


class BlockingResolver:
    def __init__(self) -> None:
        self.calls = []
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def resolve(self, request, parsed_intent, **kwargs):
        del request, kwargs
        self.calls.append(parsed_intent)
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class Gate:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def wait(self) -> None:
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class RecordingTopology:
    def __init__(self, *, gate: Gate | None = None) -> None:
        self.gate = gate
        self.calls: list[str] = []
        self.segment_calls: list[str] = []

    async def get_segment(self, comid, *, deadline=None):
        del deadline
        self.segment_calls.append(comid)
        return None

    async def get_direct_upstreams(
        self,
        comid,
        max_distance_km,
        *,
        deadline=None,
    ):
        del max_distance_km, deadline
        self.calls.append(comid)
        if self.gate is not None:
            await self.gate.wait()
        if comid != TARGET:
            return ()
        return (
            RiverSegment(
                comid=UPSTREAM,
                location=GeoPoint(latitude=39.0, longitude=-77.0),
                distance_to_downstream_km=2.5,
                downstream_comids=(TARGET,),
            ),
        )


class RecordingInventory:
    def __init__(
        self,
        *,
        covered_comid: str = UPSTREAM,
        gate: Gate | None = None,
    ) -> None:
        self.covered_comid = covered_comid
        self.gate = gate
        self.calls: list[str] = []

    async def check_availability(self, request, *, deadline=None):
        del deadline
        self.calls.append(request.comid)
        if self.gate is not None:
            await self.gate.wait()
        if request.comid != self.covered_comid:
            return ()
        references = tuple(
            SeriesRef(
                provider=ParameterProvider.WDFN_TIME_SERIES,
                station_id=f"USGS-{request.comid}",
                parameter=unit.parameter,
                channel="continuous",
                series_identity=(
                    f"series-{request.comid}-{unit.parameter.code}-{unit.segment_index}"
                ),
                coverage_source=(
                    AvailabilityCoverageSource.HISTORICAL_TIME_SERIES_ENVELOPE
                ),
                covered_units=(unit,),
            )
            for unit in request.coverage_units
        )
        return (
            StationAvailability(
                station=MonitoringStation(
                    station_id=f"USGS-{request.comid}",
                    source="nwissite",
                    name="Qualified upstream station",
                    location=None,
                    associated_comid=request.comid,
                ),
                comid=request.comid,
                providers=(ParameterProvider.WDFN_TIME_SERIES,),
                covered_units=request.coverage_units,
                series_refs=references,
                checked_at=NOW,
            ),
        )


def topology_builder(topology, inventory) -> HydrologyGraphBuilder:
    return HydrologyGraphBuilder(
        topology=topology,
        station_inventory=inventory,
        limits=TopologyLimits(max_distance_km=100, max_hops=12, max_nodes=128),
        max_concurrency=8,
        stage_timeout_seconds=1,
    )


class GateTopologyWriteStore(InMemoryTranscriptStore):
    def __init__(self) -> None:
        super().__init__()
        self.topology_write_started = asyncio.Event()
        self.release_topology_write = asyncio.Event()

    async def write_batch(self, batch) -> None:
        if any(
            item.content_type == "topology_ready"
            for item in batch.transcript.items
        ):
            self.topology_write_started.set()
            await self.release_topology_write.wait()
        await super().write_batch(batch)


class FailingTopologyBuilder:
    def __init__(self, failure: Exception) -> None:
        self.failure = failure
        self.calls = 0

    async def build(self, context):
        del context
        self.calls += 1
        raise self.failure


class CompleteAfterTopologyStage:
    async def execute(
        self,
        context,
        analysis_context,
        graph,
        *,
        topology_item_id,
    ) -> None:
        del context, analysis_context, graph, topology_item_id


@asynccontextmanager
async def workflow_runtime(
    builder,
    *,
    parser=None,
    resolver=None,
    store=None,
    inline_bytes: int = 65536,
):
    parser = parser or RecordingParser()
    resolver = resolver or RecordingResolver()
    store = store or InMemoryTranscriptStore()
    settings = AppSettings(
        analysis_timezone="America/New_York",
        transcript_inline_max_bytes=inline_bytes,
        transcript_preview_bytes=min(64, inline_bytes // 2),
    )
    workflow = compose_m4_workflow(
        settings,
        parser=parser,
        context_resolver=resolver,
        topology_builder=builder,
        parameter_capabilities=load_parameter_capability_registry(),
        error_id_factory=uuid4,
    )
    workflow.bind_execution_stage(CompleteAfterTopologyStage())
    application = create_test_app(
        settings=settings,
        clock=FixedClock(),
        workflow=workflow,
        transcript_store=store,
    )
    async with application.router.lifespan_context(application):
        yield application.state.run_coordinator, parser, resolver, store


async def wait_quiesced(coordinator, run_id) -> None:
    await asyncio.wait_for(
        coordinator._runs[run_id].workflow_quiesced.wait(),
        timeout=2,
    )


def run(coroutine) -> None:
    asyncio.run(asyncio.wait_for(coroutine, timeout=5))


def test_full_workflow_commits_one_context_and_graph_with_atomic_progress() -> None:
    async def exercise() -> None:
        topology = RecordingTopology()
        inventory = RecordingInventory()
        store = GateTopologyWriteStore()
        async with workflow_runtime(
            topology_builder(topology, inventory),
            store=store,
            inline_bytes=256,
        ) as (coordinator, parser, resolver, active_store):
            created = await coordinator.create_run(create_request_bytes())
            await asyncio.wait_for(store.topology_write_started.wait(), timeout=1)
            runtime = coordinator._runs[created.run_id]

            assert runtime.analysis_phase is AnalysisPhase.TOPOLOGY_READY
            assert runtime.context_ref is not None
            assert runtime.topology_ref is not None
            finalized_ids = {
                item.item_id for item in runtime.transcript_buffer.items
            }
            assert runtime.context_ref.item_id in finalized_ids
            assert runtime.topology_ref.item_id in finalized_ids
            ready_stream = await coordinator.subscribe_run(created.run_id)
            ready_snapshot = await anext(ready_stream)
            await ready_stream.aclose()
            assert ready_snapshot.analysis_phase is AnalysisPhase.TOPOLOGY_READY
            assert ready_snapshot.context_ref == runtime.context_ref
            assert ready_snapshot.topology_ref == runtime.topology_ref
            serialized_snapshot = ready_snapshot.model_dump_json()
            assert '"nodes":' not in serialized_snapshot
            assert '"edges":' not in serialized_snapshot

            store.release_topology_write.set()
            await wait_quiesced(coordinator, created.run_id)
            terminal = await coordinator.get_snapshot(created.run_id)
            sessions = await coordinator.list_sessions(created.run_id)
            live = await coordinator.get_live_read(created.run_id)
            assert live is not None

            assert terminal.status is RunStatus.COMPLETED
            assert runtime.analysis_phase is AnalysisPhase.TERMINAL
            reconnect = await coordinator.subscribe_run(created.run_id)
            terminal_snapshot = await anext(reconnect)
            await reconnect.aclose()
            assert terminal_snapshot.analysis_phase is AnalysisPhase.TERMINAL
            assert terminal_snapshot.context_ref == ready_snapshot.context_ref
            assert terminal_snapshot.topology_ref == ready_snapshot.topology_ref
            assert len(sessions) == 1
            assert sessions[0].agent_role is AgentRole.QUERY_PARSER
            assert sessions[0].status is AgentSessionStatus.COMPLETED
            assert runtime.waterbody_session_ids == {}
            assert runtime.waterbody_participation == {
                "100": True,
                "200": True,
            }
            assert len(parser.calls) == len(resolver.calls) == 1
            assert inventory.calls == [TARGET, UPSTREAM]
            assert topology.calls == [TARGET]

            content_types = tuple(
                item.content.content_type for item in live.items
            )
            assert content_types == (
                "analysis_request",
                "lifecycle_event",
                "lifecycle_event",
                "query_parser_input",
                "query_parser_message",
                "query_understood",
                "lifecycle_event",
                "topology_ready",
                "lifecycle_event",
            )
            context_item = next(
                item for item in live.items
                if item.content.content_type == "query_understood"
            )
            topology_item = next(
                item for item in live.items
                if item.content.content_type == "topology_ready"
            )
            assert context_item.session_id == sessions[0].session_id
            assert topology_item.session_id is None

            stored = await active_store.read_run(created.run_id)
            assert stored is not None
            external_ids = {item.item_id for item in stored.contents}
            assert context_item.item_id in external_ids
            assert topology_item.item_id in external_ids
            registry = create_production_registry()
            bodies = {item.item_id: item.body for item in stored.contents}
            for item in stored.items:
                if item.item_id not in {context_item.item_id, topology_item.item_id}:
                    continue
                parsed = registry.parse(item, bodies[item.item_id])
                assert parsed.body is not None

    run(exercise())


@pytest.mark.parametrize(
    ("failure", "code", "retryable"),
    [
        (
            PortUnavailableError(
                "metadata unavailable",
                operation="check_availability",
            ),
            "station_availability_failed",
            True,
        ),
        (
            PortUnavailableError(
                "NLDI unavailable",
                operation="get_direct_upstreams",
            ),
            "topology_lookup_failed",
            True,
        ),
        (
            InvalidPortResponseError(
                "invalid station metadata",
                operation="check_availability",
            ),
            "station_availability_failed",
            True,
        ),
        (
            TopologyInvariantError("topology contains a cycle"),
            "topology_invalid",
            False,
        ),
    ],
)
def test_topology_failures_keep_context_and_map_safe_terminal(
    failure,
    code,
    retryable,
) -> None:
    async def exercise() -> None:
        builder = FailingTopologyBuilder(failure)
        async with workflow_runtime(builder) as (coordinator, _, _, store):
            created = await coordinator.create_run(create_request_bytes())
            await wait_quiesced(coordinator, created.run_id)
            runtime = coordinator._runs[created.run_id]
            stored = await store.read_run(created.run_id)
            assert stored is not None

            assert runtime.snapshot.status is RunStatus.FAILED
            assert runtime.analysis_phase is AnalysisPhase.TERMINAL
            assert runtime.context_ref is not None
            assert runtime.topology_ref is None
            assert builder.calls == 1
            assert runtime.last_error.code == code
            assert runtime.last_error.failed_stage == "topology_building"
            assert runtime.last_error.retryable is retryable
            types = tuple(item.content_type for item in stored.items)
            assert "query_understood" in types
            assert "topology_ready" not in types

    run(exercise())


def test_business_failure_stops_before_topology() -> None:
    async def exercise() -> None:
        builder = FailingTopologyBuilder(AssertionError("must not run"))
        resolver = RecordingResolver(
            AnalysisContextResolutionStatus.PARAMETER_UNSUPPORTED
        )
        async with workflow_runtime(
            builder,
            resolver=resolver,
        ) as (coordinator, parser, _, _):
            created = await coordinator.create_run(create_request_bytes())
            await wait_quiesced(coordinator, created.run_id)
            runtime = coordinator._runs[created.run_id]

            assert runtime.snapshot.status is RunStatus.FAILED
            assert runtime.last_error.code == "parameter_unsupported"
            assert builder.calls == 0
            assert len(parser.calls) == 1
            assert runtime.context_ref is None
            assert runtime.topology_ref is None

    run(exercise())


def test_cancel_during_schema_repair_starts_no_resolver_or_topology() -> None:
    async def exercise() -> None:
        parser = RepairBlockingParser()
        resolver = RecordingResolver()
        builder = FailingTopologyBuilder(AssertionError("must not run"))
        async with workflow_runtime(
            builder,
            parser=parser,
            resolver=resolver,
        ) as (coordinator, _, _, _):
            created = await coordinator.create_run(create_request_bytes())
            await asyncio.wait_for(parser.repair_started.wait(), timeout=1)
            await coordinator.cancel_run(created.run_id)
            await wait_quiesced(coordinator, created.run_id)
            runtime = coordinator._runs[created.run_id]

            assert runtime.snapshot.status is RunStatus.CANCELLED
            assert len(parser.calls) == 2
            assert parser.cancelled.is_set()
            assert resolver.calls == []
            assert builder.calls == 0
            assert runtime.context_ref is None
            assert runtime.topology_ref is None

    run(exercise())


def test_cancel_during_resolver_starts_no_topology() -> None:
    async def exercise() -> None:
        resolver = BlockingResolver()
        builder = FailingTopologyBuilder(AssertionError("must not run"))
        async with workflow_runtime(
            builder,
            resolver=resolver,
        ) as (coordinator, parser, _, _):
            created = await coordinator.create_run(create_request_bytes())
            await asyncio.wait_for(resolver.started.wait(), timeout=1)
            await coordinator.cancel_run(created.run_id)
            await wait_quiesced(coordinator, created.run_id)
            runtime = coordinator._runs[created.run_id]

            assert runtime.snapshot.status is RunStatus.CANCELLED
            assert len(parser.calls) == len(resolver.calls) == 1
            assert resolver.cancelled.is_set()
            assert builder.calls == 0
            assert runtime.context_ref is None
            assert runtime.topology_ref is None

    run(exercise())


@pytest.mark.parametrize("blocked_stage", ["availability", "bfs"])
def test_cancel_during_topology_keeps_context_and_starts_no_late_work(
    blocked_stage,
) -> None:
    async def exercise() -> None:
        gate = Gate()
        topology = RecordingTopology(gate=gate if blocked_stage == "bfs" else None)
        inventory = RecordingInventory(
            covered_comid="999",
            gate=gate if blocked_stage == "availability" else None,
        )
        async with workflow_runtime(
            topology_builder(topology, inventory)
        ) as (coordinator, _, _, store):
            created = await coordinator.create_run(create_request_bytes())
            await asyncio.wait_for(gate.entered.wait(), timeout=1)
            cancelled = await coordinator.cancel_run(created.run_id)
            await wait_quiesced(coordinator, created.run_id)
            stored = await store.read_run(created.run_id)
            runtime = coordinator._runs[created.run_id]
            assert stored is not None

            assert cancelled.status is RunStatus.CANCELLED
            assert gate.cancelled.is_set()
            assert runtime.context_ref is not None
            assert runtime.topology_ref is None
            assert runtime.analysis_phase is AnalysisPhase.TERMINAL
            assert "topology_ready" not in {
                item.content_type for item in stored.items
            }
            assert runtime.waterbody_session_ids == {}

    run(exercise())


def test_same_idempotency_key_runs_parser_and_providers_once() -> None:
    async def exercise() -> None:
        topology = RecordingTopology()
        inventory = RecordingInventory()
        key = uuid4()
        async with workflow_runtime(
            topology_builder(topology, inventory)
        ) as (coordinator, parser, resolver, _):
            first, second = await asyncio.gather(
                coordinator.create_run(create_request_bytes(), idempotency_key=key),
                coordinator.create_run(create_request_bytes(), idempotency_key=key),
            )
            assert first.run_id == second.run_id
            await wait_quiesced(coordinator, first.run_id)
            replay = await coordinator.create_run(
                create_request_bytes(),
                idempotency_key=key,
            )

            assert replay.run_id == first.run_id
            assert len(parser.calls) == len(resolver.calls) == 1
            assert inventory.calls == [TARGET, UPSTREAM]
            assert topology.calls == [TARGET]

    run(exercise())
