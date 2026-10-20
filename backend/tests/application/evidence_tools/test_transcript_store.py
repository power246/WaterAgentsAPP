import asyncio
import json
from datetime import UTC, datetime, timedelta
from functools import wraps
from uuid import UUID, uuid4

from app.api.sse.response import create_session_event_response
from app.application.analysis_runs.coordinator import RunCoordinator
from app.application.analysis_runs.models import AgentRole, TranscriptItemIdentity
from app.application.analysis_runs.persistence import PersistenceState
from app.application.analysis_runs.workflow import WorkflowContext
from app.application.evidence_tools import (
    build_collection_manifest,
    EvidenceBundleBuilder,
    EvidenceCollectionService,
    EvidencePolicyClass,
    EvidenceTranscriptStore,
    assemble_evidence_session_scope,
)
from app.application.transcripts.read_models import ReadView
from app.application.transcripts.reader import TranscriptReader
from app.application.transcripts.registry import create_production_registry
from app.application.transcripts.stream_models import TranscriptItemProcessRecord
from app.domain import (
    CollectionStatus,
    EvidenceToolCounts,
    EvidenceToolExecutionStatus,
    EvidenceToolRequest,
    EvidenceToolResult,
    EvidenceFragmentReference,
    ReferenceScope,
    HistoricalCaseRef,
    HistoricalTranscriptRecord,
    MemoryMode,
)
from app.infrastructure.config import AppSettings, load_parameter_capability_registry
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from tests.application.evidence_tools.test_collection_service import (
    batch,
    collection,
    observation,
)
from tests.m4_support import MODEL_CATALOG, create_request_bytes, create_test_app
from tests.application.evidence_tools.conftest import make_context, make_scope
from tests.domain.test_evidence import sample_fragments, valid_source_records


def run_async(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        return asyncio.run(asyncio.wait_for(function(*args, **kwargs), timeout=15))

    return wrapped


class StepClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 10, 1, tzinfo=UTC)

    def now(self) -> datetime:
        value = self.value
        self.value += timedelta(microseconds=1)
        return value


class HoldingWorkflow:
    def __init__(self) -> None:
        self.calls: asyncio.Queue[WorkflowContext] = asyncio.Queue()
        self.release = asyncio.Event()

    async def run(self, context: WorkflowContext) -> None:
        self.calls.put_nowait(context)
        await self.release.wait()
        context.cancellation.checkpoint()


class GatedFragmentStore(InMemoryTranscriptStore):
    def __init__(self) -> None:
        super().__init__()
        self.fragment_write_started = asyncio.Event()
        self.release_fragment_write = asyncio.Event()
        self.item_reads = 0

    async def write_batch(self, batch) -> None:
        if any(
            item.content_type == "evidence_fragment"
            for item in batch.transcript.items
        ):
            self.fragment_write_started.set()
            await self.release_fragment_write.wait()
        await super().write_batch(batch)

    async def read_run_items(self, run_id, item_ids):
        self.item_reads += 1
        return await super().read_run_items(run_id, item_ids)


class RecordingOperations:
    def __init__(self) -> None:
        self.finalized = []

    async def reserve_transcript_item(
        self,
        run_id,
        *,
        session_id=None,
        request_id=None,
        operation_id=None,
    ):
        del request_id
        return TranscriptItemIdentity(
            item_id=uuid4(),
            run_id=UUID(run_id),
            session_id=UUID(session_id) if session_id is not None else None,
            transcript_sequence=1,
            created_at=datetime(2026, 10, 8, tzinfo=UTC),
            operation_id=UUID(operation_id) if operation_id is not None else None,
        )

    async def finalize_transcript_item(
        self,
        run_id,
        item_id,
        content_type,
        content_schema_version,
        body,
        *,
        rag_consumption_sources=(),
    ):
        self.finalized.append(
            (
                run_id,
                item_id,
                content_type,
                content_schema_version,
                body,
                rag_consumption_sources,
            )
        )


async def wait_until(predicate, timeout: float = 2) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


@run_async
async def test_evidence_read_waits_for_commit_and_reads_selected_stored_item(
    monkeypatch,
):
    store = GatedFragmentStore()
    workflow = HoldingWorkflow()
    coordinator = RunCoordinator(
        StepClock(),
        model_catalog=MODEL_CATALOG,
        transcript_store=store,
        workflow=workflow,
        shutdown_grace_period=0,
        shutdown_save_grace_period=0,
    )

    class NoLiveReads:
        async def get_live_read(self, run_id):
            raise AssertionError("evidence read used the live run")

    read_task = None
    try:
        created = await coordinator.create_run(create_request_bytes())
        await workflow.calls.get()
        session = await coordinator.prepare_agent_session(
            created.run_id,
            agent_role=AgentRole.WATERBODY,
            target_comid="123",
        )
        await coordinator.mark_agent_session_running(
            created.run_id, session.session_id
        )
        await wait_until(
            lambda: coordinator._transcript_writer.info_locked(
                coordinator._runs[created.run_id]
            ).state is PersistenceState.UP_TO_DATE
        )
        scope = make_scope(
            run_id=str(created.run_id), session_id=str(session.session_id)
        )
        registry = create_production_registry()

        def reject_read_redaction(*_):
            raise AssertionError("persisted evidence was redacted again")

        monkeypatch.setattr(registry, "safe_read_body", reject_read_redaction)
        reader = TranscriptReader(
            NoLiveReads(), store, registry,
            error_id_factory=uuid4,
            inline_content_bytes=65536, preview_bytes=2048,
        )
        transcript = EvidenceTranscriptStore(
            operations=coordinator,
            reader=reader,
            capability_registry=load_parameter_capability_registry(),
        )
        fragment = sample_fragments()[4].model_copy(
            update={"target_comid": "123"}
        )
        identity = await coordinator.reserve_transcript_item(
            created.run_id, session_id=session.session_id,
        )
        await coordinator.finalize_transcript_item(
            created.run_id, identity.item_id, "evidence_fragment", 1, fragment,
        )
        reference = EvidenceFragmentReference(
            item_id=str(identity.item_id), evidence_id=fragment.evidence_id,
        )
        read_task = asyncio.create_task(transcript.read_fragment(scope, reference))
        await asyncio.wait_for(store.fragment_write_started.wait(), 2)
        await asyncio.sleep(0)
        assert not read_task.done()
        assert store.item_reads == 0
        store.release_fragment_write.set()
        assert await asyncio.wait_for(read_task, 2) == fragment
        assert store.item_reads == 1
    finally:
        store.release_fragment_write.set()
        workflow.release.set()
        if read_task is not None:
            await asyncio.gather(read_task, return_exceptions=True)
        await coordinator.shutdown()


async def collect_response(response):
    messages = []

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    await response(
        {"type": "http", "asgi": {"spec_version": "2.4"}},
        receive,
        send,
    )
    return messages


def decoded_frames(messages):
    decoded = []
    for message in messages:
        if not message.get("body"):
            continue
        lines = message["body"].decode("utf-8").splitlines()
        offset = 1 if lines and lines[0].startswith("id: ") else 0
        decoded.append((
            lines[offset].removeprefix("event: "),
            json.loads(lines[offset + 1].removeprefix("data: ")),
        ))
    return decoded


@run_async
async def test_historical_source_item_hands_consumption_ownership_to_atomic_writer():
    run_id = str(uuid4())
    session_id = str(uuid4())
    scope = make_scope(
        run_id=run_id,
        session_id=session_id,
        context=make_context(memory_mode=MemoryMode.MEMORY_AUGMENTED),
    )
    source_generation_id = uuid4()
    source_case_id = uuid4()
    source_result_item_id = uuid4()
    original = valid_source_records()[-1]
    assert isinstance(original, HistoricalTranscriptRecord)
    historical = original.model_copy(
        update={
            "case_ref": HistoricalCaseRef(
                generation_id=str(source_generation_id),
                case_id=str(source_case_id),
                run_id=str(uuid4()),
                session_id=str(uuid4()),
                target_comid=original.case_ref.target_comid,
                analysis_context_item_id=str(uuid4()),
                result_item_id=str(source_result_item_id),
            )
        }
    )
    provider_batch = batch(
        scope,
        (historical,),
        operation_id=str(uuid4()),
    )
    manifest = build_collection_manifest(
        manifest_id="manifest:historical",
        provider="transcript",
        channel="historical",
        request_range=scope.query_envelope,
        requested_series=("strategy:exact_comid_parameter_window",),
        windows=(scope.time_windows[0],),
        batch=provider_batch,
        completion_applies_to_all_windows=True,
    )
    operations = RecordingOperations()
    transcript = EvidenceTranscriptStore(
        operations=operations,
        reader=object(),
        capability_registry=load_parameter_capability_registry(),
    )

    await transcript.write_collection(
        scope,
        operation_id=provider_batch.operation_id,
        manifests=(manifest,),
        records=(historical,),
    )

    sources = operations.finalized[0][-1]
    assert len(sources) == 1
    assert sources[0].source_generation_id == source_generation_id
    assert sources[0].source_case_id == source_case_id
    assert sources[0].source_result_item_id == source_result_item_id


@run_async
async def test_evidence_content_uses_existing_writer_reader_history_and_session_stream(
    context_factory,
    graph_factory,
):
    workflow = HoldingWorkflow()
    persistence = InMemoryTranscriptStore()
    capabilities = load_parameter_capability_registry()
    application = create_test_app(
        settings=AppSettings(
            _env_file=None,
            transcript_inline_max_bytes=256,
            transcript_preview_bytes=64,
        ),
        clock=StepClock(),
        workflow=workflow,
        transcript_store=persistence,
        model_catalog=MODEL_CATALOG,
        parameter_capability_registry=capabilities,
        shutdown_grace_period=0,
    )
    async with application.router.lifespan_context(application):
        coordinator = application.state.run_coordinator
        created = await coordinator.create_run(create_request_bytes("evidence"))
        workflow_context = await workflow.calls.get()

        parser = await coordinator.prepare_agent_session(
            created.run_id,
            agent_role=AgentRole.QUERY_PARSER,
        )
        await coordinator.mark_agent_session_running(created.run_id, parser.session_id)
        context = context_factory()
        context_identity = await coordinator.reserve_transcript_item(
            created.run_id,
            session_id=parser.session_id,
        )
        await coordinator.finalize_transcript_item(
            created.run_id,
            context_identity.item_id,
            "query_understood",
            1,
            context,
        )
        graph = graph_factory()
        topology_identity = await coordinator.reserve_transcript_item(created.run_id)
        await coordinator.finalize_transcript_item(
            created.run_id,
            topology_identity.item_id,
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
        scope = assemble_evidence_session_scope(
            run_id=str(created.run_id),
            session_id=str(session.session_id),
            session_fact=ReferenceScope(
                item_id=str(session.session_id),
                run_id=str(created.run_id),
                session_id=str(session.session_id),
            ),
            analysis_context_ref=ReferenceScope(
                item_id=str(context_identity.item_id),
                run_id=str(created.run_id),
            ),
            topology_ref=ReferenceScope(
                item_id=str(topology_identity.item_id),
                run_id=str(created.run_id),
            ),
            context=context,
            topology=graph,
            agent_comid="123",
            registry=capabilities,
            cancel_token=workflow_context.cancellation,
        )
        transcript = EvidenceTranscriptStore(
            operations=coordinator,
            reader=application.state.transcript_reader,
            capability_registry=capabilities,
        )
        operation_id = str(uuid4())
        request = EvidenceToolRequest(
            tool_call_id="call-001",
            session_id=scope.session_id,
            tool_name="collect_water_observations",
            purpose="local_station_preflight",
            window_ids=tuple(item.window_id for item in scope.time_windows),
            parameter_keys=(
                scope.requested_parameters[0],
                scope.supporting_parameters[0],
            ),
        )
        await transcript.write_call(
            scope,
            request,
            operation_id=operation_id,
            policy=EvidencePolicyClass.CONDITIONAL_MANDATORY,
        )
        records = (
            observation(scope, 1).model_copy(
                update={
                    "source_uri": (
                        "https://api.waterdata.usgs.gov/ogcapi/items/record-1"
                        "?api_key=private-value"
                    )
                }
            ),
            observation(scope, 2).model_copy(
                update={"source_uri": "https://arbitrary.example/record-2"}
            ),
        )
        artifacts = await EvidenceCollectionService(
            registry=capabilities,
            writer=transcript,
        ).process(
            scope,
            operation_id=operation_id,
            collections=(
                collection(
                    scope,
                    batch(scope, records, operation_id="source-page"),
                ),
            ),
        )
        result = EvidenceToolResult(
            tool_call_id=request.tool_call_id,
            operation_id=operation_id,
            tool_name=request.tool_name,
            execution_status=EvidenceToolExecutionStatus.COMPLETED,
            collection_status=CollectionStatus.AVAILABLE,
            manifest_refs=artifacts.manifest_refs,
            fragment_refs=artifacts.fragment_refs,
            collection_fact_refs=artifacts.collection_fact_refs,
            source_record_refs=artifacts.source_record_refs,
            counts=EvidenceToolCounts(
                requests=1,
                pages=1,
                records=2,
                fragments=len(artifacts.fragment_refs) + len(artifacts.collection_fact_refs),
            ),
            completed_at=StepClock().now(),
        )
        result_ref = await transcript.write_result(scope, result)
        built = await EvidenceBundleBuilder(
            reader=transcript,
            writer=transcript,
        ).build_and_write(
            scope,
            fragment_refs=(*artifacts.fragment_refs, *artifacts.collection_fact_refs),
            tool_result_refs=(result_ref,),
            created_at=StepClock().now(),
        )
        bundle, bundle_ref = built.bundle, built.reference

        runtime = coordinator._runs[created.run_id]
        await wait_until(
            lambda: coordinator._transcript_writer.info_locked(runtime).state
            is PersistenceState.UP_TO_DATE
        )
        session_read = await application.state.transcript_reader.get_session_transcript(
            created.run_id,
            session.session_id,
            ReadView.AUTO,
        )
        registered = await application.state.transcript_reader.read_registered_run(
            created.run_id, ReadView.AUTO,
        )
        registered_items = tuple(
            item for item in registered.items
            if item.item.session_id == session.session_id and item.body is not None
        )
        assert tuple(item.item.item_id for item in registered_items) == tuple(
            item.item_id for item in session_read.items
        )
        assert all(item.body is not None for item in registered_items)
        evidence_items = tuple(
            item
            for item in session_read.items
            if item.content_type.startswith("evidence_")
        )
        evidence_types = tuple(item.content_type for item in evidence_items)
        assert all(item.parse_status.value == "ok" for item in evidence_items)
        assert evidence_types[0] == "evidence_tool_call"
        assert evidence_types.index("evidence_source_records") < evidence_types.index(
            "evidence_fragment"
        )
        assert evidence_types[-2:] == ("evidence_tool_result", "evidence_bundle")
        assert bundle_ref.item_id == str(evidence_items[-1].item_id)
        assert set(bundle.referenced_item_ids()) <= {
            str(item.item_id) for item in (*session_read.items, *session_read.referenced_items)
        }

        source_item = next(
            item for item in evidence_items if item.content_type == "evidence_source_records"
        )
        assert source_item.content.kind == "reference"
        public_preview = source_item.content.preview_head + source_item.content.preview_tail
        assert "private-value" not in public_preview
        assert "api_key" not in public_preview
        full = await application.state.transcript_reader.get_content(
            created.run_id,
            source_item.item_id,
            ReadView.AUTO,
        )
        assert "private-value" not in full.text
        assert "[REDACTED]" in full.text
        parsed_full = json.loads(full.text)
        assert len(parsed_full["records"]) == 2
        inline_reader = TranscriptReader(
            coordinator,
            persistence,
            create_production_registry(),
            error_id_factory=uuid4,
            inline_content_bytes=1_000_000,
            preview_bytes=32,
        )
        inline_read = await inline_reader.get_session_transcript(
            created.run_id,
            session.session_id,
            ReadView.AUTO,
        )
        inline_source = next(
            item
            for item in inline_read.items
            if item.content_type == "evidence_source_records"
        )
        assert inline_source.content.kind == "inline"
        assert inline_source.content.body["records"][0]["source_uri"] == (
            "https://api.waterdata.usgs.gov/ogcapi/items/record-1"
        )
        assert inline_source.content.body["records"][1]["source_uri"] is None
        full_evidence_bodies = []
        for item in evidence_items:
            content = await application.state.transcript_reader.get_content(
                created.run_id,
                item.item_id,
                ReadView.AUTO,
            )
            full_evidence_bodies.append(content.text)
        assert sum('"source_uri"' in body for body in full_evidence_bodies) == 1

        source_item_id = artifacts.source_record_refs[0].item_id
        strict_history = EvidenceTranscriptStore(
            operations=coordinator,
            reader=application.state.transcript_reader,
            capability_registry=capabilities,
            read_view=ReadView.HISTORY,
        )
        assert len(await strict_history.read_source_records(scope, source_item_id)) == 2
        assert await strict_history.read_fragment(scope, artifacts.fragment_refs[0])
        scopes = await strict_history.resolve_reference_scopes(
            scope,
            (str(context_identity.item_id), source_item_id, result_ref.item_id),
        )
        assert tuple(item.session_id for item in scopes) == (
            None,
            str(session.session_id),
            str(session.session_id),
        )

        history_read = await application.state.transcript_reader.get_session_transcript(
            created.run_id,
            session.session_id,
            ReadView.HISTORY,
        )
        assert tuple(
            item.content_type
            for item in history_read.items
            if item.content_type.startswith("evidence_")
        ) == evidence_types

        stream = await coordinator.subscribe_session(
            created.run_id,
            session.session_id,
        )
        streamed_types = []
        while "evidence_bundle" not in streamed_types:
            record_or_keepalive = await anext(stream)
            if isinstance(record_or_keepalive, TranscriptItemProcessRecord):
                streamed_types.append(record_or_keepalive.item.content.content_type)
        await stream.aclose()
        assert tuple(item for item in streamed_types if item.startswith("evidence_")) == (
            evidence_types
        )

        run_stream = await coordinator.subscribe_run(created.run_id)
        run_snapshot = await anext(run_stream)
        await run_stream.aclose()
        run_payload = json.dumps(run_snapshot.model_dump(mode="json"))
        assert "tool_call_id" not in run_payload
        assert "source_uri" not in run_payload

        await coordinator.cancel_agent_session(created.run_id, session.session_id)
        await wait_until(
            lambda: coordinator._transcript_writer.info_locked(runtime).state
            is PersistenceState.UP_TO_DATE
        )
        response = await create_session_event_response(
            coordinator,
            application.state.transcript_reader,
            created.run_id,
            session.session_id,
        )
        frames = decoded_frames(await collect_response(response))
        assert {event for event, _ in frames} == {"transcript_item", "stream_end"}
        source_event = next(
            payload
            for event, payload in frames
            if event == "transcript_item"
            and payload["payload"]["item"]["content_type"]
            == "evidence_source_records"
        )
        serialized_source_event = json.dumps(source_event)
        assert "private-value" not in serialized_source_event
        assert '"kind": "reference"' in serialized_source_event

        workflow.release.set()
