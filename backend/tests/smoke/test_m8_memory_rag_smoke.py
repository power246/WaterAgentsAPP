import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import pytest

import app.main as app_main
from app.application.analysis_runs.models import AgentRole, RunStatus
from app.application.analysis_runs.workflow import WorkflowContext
from app.application.evidence_tools import (
    assemble_evidence_session_scope,
)
from app.application.reasoning import (
    WaterbodyAgentRunStatus,
    WaterbodyAgentTask,
    validate_waterbody_agent_task,
)
from app.application.reasoning.model_models import (
    StructuredOutputCompleted,
    Terminal,
    ToolCallCompleted,
    UsageReported,
    WaterbodyFinishReason,
    WaterbodyModelPhase,
)
from app.application.transcripts.read_models import ReadView
from app.domain import (
    DirectRecordPayload,
    EvidenceSourceRecords,
    HistoricalSearchStrategy,
    HistoricalTranscriptRecord,
    ItemReference,
    MemoryMode,
    ReferenceScope,
    SourceRecordReference,
)
from app.infrastructure.config import load_parameter_capability_registry
from app.infrastructure.runtime import (
    OpenAIResponsesSizeEstimator,
    WaterbodyAdapterRegistration,
    build_m8_runtime,
)
from app.infrastructure.runtime.m5 import compose_m5_runtime
from app.infrastructure.runtime.m6_test import build_m6_test_runtime
from scripts.test_database import temporary_migrated_schema
from tests.application.evidence_tools.fakes import FakeCollectionPort, complete_batch
from tests.application.evidence_tools.test_acquisition_workflow import _water_port
from tests.application.evidence_tools.conftest import (
    TARGET_PARAMETER,
    make_context,
    make_graph,
)
from tests.application.reasoning.test_runner import AdaptiveWaterbodyModelPort
from tests.application.transcript_memory.m3_support import _fragment, _result
from tests.domain.test_evidence import valid_manifest, valid_source_records
from tests.infrastructure.embeddings.fake import DeterministicEmbeddingPort
from tests.m4_support import (
    MODEL_CATALOG,
    PARSER_MODEL,
    WATERBODY_MODEL,
    create_request_bytes,
    create_test_app,
)


class QueueWorkflow:
    def __init__(self) -> None:
        self.calls: asyncio.Queue[tuple[WorkflowContext, asyncio.Event]] = (
            asyncio.Queue()
        )

    async def run(self, context: WorkflowContext) -> None:
        release = asyncio.Event()
        self.calls.put_nowait((context, release))
        await release.wait()
        context.cancellation.checkpoint()


class BoundWaterPort:
    def __init__(self) -> None:
        self._scopes = {}

    def bind(self, scope) -> None:
        for station in scope.station_refs:
            self._scopes[station.station_id] = scope

    async def collect(self, request, *, deadline=None):
        scope = self._scopes[request.station_id]
        return await _water_port(scope).collect(request, deadline=deadline)


class ScenarioWaterbodyModelPort(AdaptiveWaterbodyModelPort):
    def __init__(self) -> None:
        super().__init__()
        self.scenario = "skip"
        self._planning_turn = 0

    def select(self, scenario: str) -> int:
        self.scenario = scenario
        self._planning_turn = 0
        return len(self.calls)

    async def stream(self, request) -> AsyncIterator[object]:
        self.calls.append(request)
        values = {"operation_id": request.operation_id, "model": request.model}
        if request.phase is WaterbodyModelPhase.EVIDENCE_PLANNING:
            self._planning_turn += 1
            if self.scenario == "use" and self._planning_turn == 1:
                yield ToolCallCompleted(
                    **values,
                    provider_call_id="m8-historical-call",
                    name="search_transcript_evidence",
                    arguments=json.dumps(
                        {
                            "window_ids": ["window-000"],
                            "parameter_keys": [
                                TARGET_PARAMETER.model_dump(mode="json")
                            ],
                            "options": {
                                "historical_search_strategy": (
                                    "semantic_case_query"
                                ),
                                "record_limit": 5,
                            },
                        },
                        separators=(",", ":"),
                    ),
                )
                yield Terminal(
                    **values,
                    provider_response_id="m8-historical-tool-response",
                    finish_reason=WaterbodyFinishReason.COMPLETED,
                )
                return
            payload = {"decision": "evidence_ready"}
        else:
            payload = self._payload(request)
        yield StructuredOutputCompleted(
            **values,
            output_id=f"output:{request.operation_id}",
            value=json.dumps(payload, separators=(",", ":")),
        )
        yield UsageReported(**values, input_tokens=10, output_tokens=5)
        yield Terminal(
            **values,
            provider_response_id=f"response:{request.operation_id}",
            finish_reason=WaterbodyFinishReason.COMPLETED,
        )


@dataclass(frozen=True, slots=True)
class PreparedRun:
    run_id: UUID
    session_id: UUID
    task: object
    scope: object
    release: asyncio.Event


def _context(*, memory_mode: MemoryMode, query: str):
    return make_context(memory_mode=memory_mode).model_copy(
        update={
            "query": query,
            "parser_model": PARSER_MODEL,
            "waterbody_model": WATERBODY_MODEL,
        }
    )


async def _save_context_and_topology(
    application,
    workflow: QueueWorkflow,
    *,
    memory_mode: MemoryMode,
    query: str,
    with_upstream: bool,
):
    coordinator = application.state.run_coordinator
    created = await coordinator.create_run(
        create_request_bytes(query, memory_mode=memory_mode.value)
    )
    workflow_context, release = await workflow.calls.get()
    parser = await coordinator.prepare_agent_session(
        created.run_id,
        agent_role=AgentRole.QUERY_PARSER,
    )
    await coordinator.mark_agent_session_running(created.run_id, parser.session_id)
    context = _context(memory_mode=memory_mode, query=query)
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
    await coordinator.complete_agent_session(created.run_id, parser.session_id)
    topology = make_graph(with_upstream=with_upstream)
    topology_item = await coordinator.reserve_transcript_item(created.run_id)
    await coordinator.finalize_transcript_item(
        created.run_id,
        topology_item.item_id,
        "topology_ready",
        1,
        topology,
    )
    await coordinator.begin_waterbody_execution(created.run_id)
    return (
        created.run_id,
        workflow_context,
        release,
        context,
        context_item,
        topology,
        topology_item,
    )


async def _complete_historical_run(
    application,
    workflow: QueueWorkflow,
    *,
    query: str,
    comids: tuple[str, ...],
):
    (
        run_id,
        _workflow_context,
        release,
        context,
        _context_item,
        _topology,
        _topology_item,
    ) = await _save_context_and_topology(
        application,
        workflow,
        memory_mode=MemoryMode.STATELESS,
        query=query,
        with_upstream=len(comids) > 1,
    )
    coordinator = application.state.run_coordinator
    identities = {}
    for comid in comids:
        session = await coordinator.prepare_agent_session(
            run_id,
            agent_role=AgentRole.WATERBODY,
            target_comid=comid,
        )
        await coordinator.mark_agent_session_running(run_id, session.session_id)
        source_item = await coordinator.reserve_transcript_item(
            run_id,
            session_id=session.session_id,
        )
        manifest = valid_manifest()
        records = valid_source_records()[:2]
        await coordinator.finalize_transcript_item(
            run_id,
            source_item.item_id,
            "evidence_source_records",
            1,
            EvidenceSourceRecords(
                operation_id=manifest.operation_id,
                batch_id=manifest.manifest_id,
                manifest=manifest,
                records=records,
            ),
        )
        fragment_item = await coordinator.reserve_transcript_item(
            run_id,
            session_id=session.session_id,
        )
        source_ref = SourceRecordReference(
            item_id=str(source_item.item_id),
            record_id=records[0].record_id,
        )
        fragment = _fragment(
            context,
            target_comid=comid,
            source_record_item_id=source_item.item_id,
        ).model_copy(
            update={
                "payload": DirectRecordPayload(
                    record_ref=source_ref,
                    displayed_value="8",
                    displayed_unit=context.params_used[0].canonical_unit,
                ),
                "source_record_refs": (source_ref,),
            }
        )
        await coordinator.finalize_transcript_item(
            run_id,
            fragment_item.item_id,
            "evidence_fragment",
            1,
            fragment,
        )
        result = _result(
            context,
            target_comid=comid,
            session_id=session.session_id,
            fragment_item_id=fragment_item.item_id,
            transcript_item_refs=(
                str(fragment_item.item_id),
                str(source_item.item_id),
            ),
        )
        result = result.model_copy(update={"run_id": run_id})
        completed = await coordinator.complete_waterbody_agent_session(
            run_id,
            session.session_id,
            result,
        )
        identities[comid] = (
            str(session.session_id),
            str(completed.result_ref.item_id),
        )
    completed_run = await coordinator.complete_run(run_id)
    assert completed_run.status is RunStatus.COMPLETED
    release.set()
    await coordinator._runs[run_id].workflow_quiesced.wait()
    return run_id, identities


async def _prepare_current_run(
    application,
    workflow: QueueWorkflow,
    water: BoundWaterPort,
    *,
    memory_mode: MemoryMode,
    query: str,
) -> PreparedRun:
    (
        run_id,
        workflow_context,
        release,
        context,
        context_item,
        topology,
        topology_item,
    ) = await _save_context_and_topology(
        application,
        workflow,
        memory_mode=memory_mode,
        query=query,
        with_upstream=False,
    )
    coordinator = application.state.run_coordinator
    session = await coordinator.prepare_agent_session(
        run_id,
        agent_role=AgentRole.WATERBODY,
        target_comid=topology.target_comid,
    )
    scope = assemble_evidence_session_scope(
        run_id=str(run_id),
        session_id=str(session.session_id),
        session_fact=ReferenceScope(
            item_id=str(session.session_id),
            run_id=str(run_id),
            session_id=str(session.session_id),
        ),
        analysis_context_ref=ReferenceScope(
            item_id=str(context_item.item_id),
            run_id=str(run_id),
        ),
        topology_ref=ReferenceScope(
            item_id=str(topology_item.item_id),
            run_id=str(run_id),
        ),
        context=context,
        topology=topology,
        agent_comid=topology.target_comid,
        registry=load_parameter_capability_registry(),
        cancel_token=workflow_context.cancellation,
    )
    water.bind(scope)
    task = WaterbodyAgentTask(
        run_id=run_id,
        session_id=session.session_id,
        target_comid=topology.target_comid,
        analysis_context_ref=ItemReference(item_id=str(context_item.item_id)),
        topology_ref=ItemReference(item_id=str(topology_item.item_id)),
        evidence_scope=scope,
        waterbody_model=context.waterbody_model,
        memory_mode=context.memory_mode,
        cancellation_token=scope.cancel_token,
    )
    validated = await validate_waterbody_agent_task(
        task,
        run=await coordinator.get_snapshot(run_id),
        context=context,
        topology=topology,
        model_catalog=MODEL_CATALOG.snapshot(),
        session_reader=application.state.m5_runtime.transcript,
    )
    await coordinator.mark_agent_session_running(run_id, session.session_id)
    return PreparedRun(
        run_id=run_id,
        session_id=session.session_id,
        task=validated,
        scope=scope,
        release=release,
    )


async def _wait_for_active_jobs(runtime, minimum: int) -> None:
    for _ in range(500):
        active = await runtime.repository.read_active_generation()
        if active is not None:
            counts = await runtime.repository.counts(active.generation_id)
            if counts.completed >= minimum:
                return
        await asyncio.sleep(0.01)
    raise AssertionError("M8 worker did not activate all historical projections")


async def _finish_cancelled_fixture(application, prepared: PreparedRun) -> None:
    await application.state.run_coordinator.cancel_run(prepared.run_id)
    prepared.release.set()
    await application.state.run_coordinator._runs[
        prepared.run_id
    ].workflow_quiesced.wait()


@pytest.mark.skipif(
    not os.environ.get("WATERAGENTS_TEST_DB_NAME"),
    reason="dedicated PostgreSQL test database was not configured",
)
def test_production_entrypoint_indexes_searches_consumes_and_restarts(
    monkeypatch,
) -> None:
    water = BoundWaterPort()
    model = ScenarioWaterbodyModelPort()

    @asynccontextmanager
    async def deterministic_m5_builder(settings, **kwargs):
        yield compose_m5_runtime(
            settings,
            operations=kwargs["operations"],
            transcript_reader=kwargs["transcript_reader"],
            capability_registry=kwargs["capability_registry"],
            water_port=water,
            weather_port=FakeCollectionPort(
                lambda request: complete_batch(request)
            ),
            facility_port=FakeCollectionPort(
                lambda request: complete_batch(request)
            ),
            clock=kwargs["clock"],
            upstream_port=kwargs.get("upstream_results"),
            transcript_port=kwargs["transcript_port"],
            upstream_results=kwargs.get("upstream_results"),
        )

    @asynccontextmanager
    async def deterministic_m8_builder(settings, **kwargs):
        async with build_m8_runtime(
            settings,
            embedding_port=DeterministicEmbeddingPort(),
            lease_owner_factory=lambda: "m8-e2e-worker",
            **kwargs,
        ) as runtime:
            yield runtime

    @asynccontextmanager
    async def deterministic_m6_builder(settings, **kwargs):
        async with build_m6_test_runtime(
            settings,
            registrations=(
                WaterbodyAdapterRegistration(
                    provider="test-provider",
                    port=model,
                    size_estimator=OpenAIResponsesSizeEstimator(),
                ),
            ),
            **kwargs,
        ) as runtime:
            yield runtime

    monkeypatch.setattr(app_main, "build_m5_runtime", deterministic_m5_builder)

    async def exercise(settings) -> None:
        resolved = settings.model_copy(
            update={
                "rag_reconciliation_interval_seconds": 1.0,
                "rag_worker_shutdown_grace_seconds": 1,
                "live_state_grace_seconds": 60,
                "db_operation_timeout_seconds": 30,
                "idempotency_wait_timeout_seconds": 30,
            }
        )
        workflow = QueueWorkflow()
        application = create_test_app(
            resolved,
            workflow=workflow,
            m6_runtime_builder=deterministic_m6_builder,
            m8_runtime_builder=deterministic_m8_builder,
            shutdown_grace_period=0,
        )
        async with application.router.lifespan_context(application):
            first_run, first_cases = await _complete_historical_run(
                application,
                workflow,
                query="Historical multi-waterbody dissolved oxygen query.",
                comids=("123", "200"),
            )
            second_run, second_cases = await _complete_historical_run(
                application,
                workflow,
                query="Historical single-waterbody dissolved oxygen query.",
                comids=("123",),
            )
            await _wait_for_active_jobs(application.state.m8_runtime, 3)

            model_start = model.select("use")
            current = await _prepare_current_run(
                application,
                workflow,
                water,
                memory_mode=MemoryMode.MEMORY_AUGMENTED,
                query="Use relevant memory, then analyze current dissolved oxygen.",
            )
            outcome = await application.state.m6_runtime.runner.run(
                current.task,
                deadline=(
                    application.state.m6_runtime.runner.session_deadline()
                    - timedelta(seconds=1)
                ),
            )
            assert outcome.status is WaterbodyAgentRunStatus.COMPLETED
            assert outcome.result is not None
            current_snapshot = await application.state.run_coordinator.complete_run(
                current.run_id
            )
            assert current_snapshot.status is RunStatus.COMPLETED
            current.release.set()
            await application.state.run_coordinator._runs[
                current.run_id
            ].workflow_quiesced.wait()

            model_calls = model.calls[model_start:]
            planning = tuple(
                request
                for request in model_calls
                if request.phase is WaterbodyModelPhase.EVIDENCE_PLANNING
            )
            assert len(planning) == 2
            assert "search_transcript_evidence" in {
                item.name for item in planning[0].tool_descriptors
            }

            registered = await application.state.transcript_reader.read_registered_run(
                current.run_id,
                ReadView.HISTORY,
            )
            historical_records = tuple(
                record
                for item in registered.items
                if isinstance(item.body, EvidenceSourceRecords)
                for record in item.body.records
                if isinstance(record, HistoricalTranscriptRecord)
            )
            assert len(historical_records) == 2
            expected = {
                (str(first_run), *first_cases["123"]),
                (str(second_run), *second_cases["123"]),
            }
            assert {
                (
                    record.case_ref.run_id,
                    record.case_ref.session_id,
                    record.case_ref.result_item_id,
                )
                for record in historical_records
            } == expected
            assert all(
                record.matched_by.strategy
                is HistoricalSearchStrategy.SEMANTIC_CASE_QUERY
                and record.matched_by.vector_similarity is not None
                and record.matched_by.vector_similarity >= 0.78
                and record.rag_score > 0
                and record.result_projection.cells
                and {
                    cell.parameter for cell in record.result_projection.cells
                } == {TARGET_PARAMETER}
                and len(record.supporting_fragments) <= 2
                and all(
                    fragment.applicability.value == "contextual_only"
                    for fragment in record.supporting_fragments
                )
                for record in historical_records
            )
            historical_fragments = tuple(
                item.body
                for item in registered.items
                if getattr(item.body, "data_type", None)
                == "historical_inference"
            )
            assert historical_fragments
            assert all(
                fragment.applicability.value == "contextual_only"
                for fragment in historical_fragments
            )
            historical_evidence_ids = {
                fragment.evidence_id for fragment in historical_fragments
            }
            result_evidence_ids = {
                reference.evidence_id
                for reference in outcome.result.evidence_chain.references
            }
            assert result_evidence_ids - historical_evidence_ids
            links = await application.state.m8_runtime.repository.read_consumption_links(
                str(current.run_id)
            )
            assert len(links) == len(historical_records)
            assert {
                str(link.source_result_item_id) for link in links
            } == {
                record.case_ref.result_item_id for record in historical_records
            }
            assert any(
                item.item.content_type == "evidence_bundle"
                for item in registered.items
            )
            assert any(
                item.item.content_type == "inference_result"
                and item.item.session_id == current.session_id
                for item in registered.items
            )

            run_events = await application.state.run_coordinator.subscribe_run(
                current.run_id
            )
            assert (await anext(run_events)).status is RunStatus.COMPLETED
            await run_events.aclose()
            session_events = await application.state.run_coordinator.subscribe_session(
                current.run_id,
                current.session_id,
                after_sequence=0,
            )
            assert await anext(session_events) is not None
            await session_events.aclose()

            memory_skip_start = model.select("skip")
            memory_skip = await _prepare_current_run(
                application,
                workflow,
                water,
                memory_mode=MemoryMode.MEMORY_AUGMENTED,
                query="Analyze current dissolved oxygen without using memory.",
            )
            skipped = await application.state.m6_runtime.runner.run(
                memory_skip.task,
                deadline=(
                    application.state.m6_runtime.runner.session_deadline()
                    - timedelta(seconds=1)
                ),
            )
            assert skipped.status is WaterbodyAgentRunStatus.COMPLETED
            skip_planning = next(
                request
                for request in model.calls[memory_skip_start:]
                if request.phase is WaterbodyModelPhase.EVIDENCE_PLANNING
            )
            assert "search_transcript_evidence" in {
                item.name for item in skip_planning.tool_descriptors
            }
            await _finish_cancelled_fixture(application, memory_skip)

            stateless_start = model.select("skip")
            stateless = await _prepare_current_run(
                application,
                workflow,
                water,
                memory_mode=MemoryMode.STATELESS,
                query="Analyze current dissolved oxygen statelessly.",
            )
            stateless_outcome = await application.state.m6_runtime.runner.run(
                stateless.task,
                deadline=(
                    application.state.m6_runtime.runner.session_deadline()
                    - timedelta(seconds=1)
                ),
            )
            assert stateless_outcome.status is WaterbodyAgentRunStatus.COMPLETED
            stateless_planning = next(
                request
                for request in model.calls[stateless_start:]
                if request.phase is WaterbodyModelPhase.EVIDENCE_PLANNING
            )
            assert "search_transcript_evidence" not in {
                item.name for item in stateless_planning.tool_descriptors
            }
            await _finish_cancelled_fixture(application, stateless)

        restarted_workflow = QueueWorkflow()
        restarted = create_test_app(
            resolved,
            workflow=restarted_workflow,
            m6_runtime_builder=deterministic_m6_builder,
            m8_runtime_builder=deterministic_m8_builder,
            shutdown_grace_period=0,
        )
        async with restarted.router.lifespan_context(restarted):
            active = await restarted.state.m8_runtime.repository.read_active_generation()
            assert active is not None
            assert (
                await restarted.state.m8_runtime.repository.counts(
                    active.generation_id
                )
            ).completed >= 3
            reloaded = await restarted.state.transcript_reader.read_registered_run(
                current.run_id,
                ReadView.HISTORY,
            )
            assert reloaded.run.status is RunStatus.COMPLETED
            assert await restarted.state.m8_runtime.repository.read_consumption_links(
                str(current.run_id)
            )
            cleanup = await restarted.state.m8_runtime.operations.inspect_cleanup_dependencies(
                (str(first_run),)
            )
            assert cleanup.cascade_required
            assert str(current.run_id) in cleanup.delete_order

    with temporary_migrated_schema() as settings:
        asyncio.run(asyncio.wait_for(exercise(settings), timeout=90))
