import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from app.application.analysis_runs.models import AgentRole, AgentSessionStatus
from app.application.analysis_runs.persistence import PersistenceState
from app.application.analysis_runs.workflow import WorkflowContext
from app.application.reasoning.model_models import (
    StructuredOutputCompleted,
    Terminal,
    UsageReported,
    VisibleTextDelta,
    WaterbodyFinishReason,
    WaterbodyJsonSchema,
    WaterbodyMessageRole,
    WaterbodyModelEvent,
    WaterbodyModelMessage,
    WaterbodyModelPhase,
    WaterbodyModelRequest,
)
from app.application.reasoning.model_protocol import consume_waterbody_model_stream
from app.application.reasoning.transcript_models import ReasoningDecisionType
from app.application.reasoning.transcript_store import ReasoningTranscriptStore
from app.application.transcripts.read_models import ReadView
from app.application.transcripts.stream_models import (
    MessageDeltaProcessRecord,
    MessageFinalizedProcessRecord,
    MessageStartedProcessRecord,
    TranscriptItemProcessRecord,
)
from app.domain import ItemReference
from app.infrastructure.config import AppSettings
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from tests.application.evidence_tools.conftest import MODEL
from tests.application.evidence_tools.fakes import FixedClock
from tests.application.reasoning.test_planning_loop import _fixture
from tests.application.reasoning.test_runner import AdaptiveWaterbodyModelPort, _runner
from tests.m4_support import MODEL_CATALOG, create_request_bytes, create_test_app


class HoldingWorkflow:
    def __init__(self) -> None:
        self.calls: asyncio.Queue[WorkflowContext] = asyncio.Queue()
        self.release = asyncio.Event()

    async def run(self, context: WorkflowContext) -> None:
        self.calls.put_nowait(context)
        await self.release.wait()
        context.cancellation.checkpoint()


class NeverCancelled:
    def is_requested(self) -> bool:
        return False


class StreamingModelPort:
    async def stream(
        self,
        request: WaterbodyModelRequest,
    ) -> AsyncIterator[WaterbodyModelEvent]:
        values = {"operation_id": request.operation_id, "model": request.model}
        yield VisibleTextDelta(
            **values,
            message_id="message-one",
            text="First visible message. ",
        )
        yield VisibleTextDelta(
            **values,
            message_id="message-two",
            text="The credential is super-",
        )
        yield VisibleTextDelta(
            **values,
            message_id="message-one",
            text="Finished.",
        )
        yield VisibleTextDelta(
            **values,
            message_id="message-two",
            text="secret and must be redacted.",
        )
        yield StructuredOutputCompleted(
            **values,
            output_id="output-one",
            value='{"decision":"evidence_ready","note":"super-secret"}',
        )
        yield UsageReported(**values, input_tokens=21, output_tokens=8)
        yield Terminal(
            **values,
            provider_response_id="provider-response-one",
            finish_reason=WaterbodyFinishReason.COMPLETED,
        )


async def _wait_persisted(coordinator, run_id) -> None:
    runtime = coordinator._runs[run_id]
    async with asyncio.timeout(3):
        while (
            coordinator._transcript_writer.info_locked(runtime).state
            is not PersistenceState.UP_TO_DATE
        ):
            await asyncio.sleep(0)


def _model_request(operation_id, bundle_item_id) -> WaterbodyModelRequest:
    schema = WaterbodyJsonSchema(
        name="evidence_planning_decision",
        schema_text=json.dumps({
            "type": "object",
            "properties": {"decision": {"type": "string"}},
            "required": ["decision"],
            "additionalProperties": False,
        }),
    )
    return WaterbodyModelRequest(
        operation_id=operation_id,
        phase=WaterbodyModelPhase.EVIDENCE_PLANNING,
        model=MODEL,
        system_prompt="Use only the bounded evidence scope; omit super-secret.",
        messages=(WaterbodyModelMessage(
            message_id="request-message",
            role=WaterbodyMessageRole.USER,
            text="Decide whether the evidence is ready.",
        ),),
        response_schema=schema,
        prompt_policy_digest="sha256:policy",
        scope_digest="sha256:scope",
        evidence_bundle_ref=ItemReference(item_id=str(bundle_item_id)),
        deadline=datetime(2026, 10, 2, 1, tzinfo=UTC),
    )


def test_recording_port_uses_existing_message_stream_and_typed_items() -> None:
    async def exercise() -> None:
        workflow = HoldingWorkflow()
        persistence = InMemoryTranscriptStore()
        application = create_test_app(
            settings=AppSettings(
                _env_file=None,
                transcript_inline_max_bytes=64 * 1024,
                transcript_preview_bytes=128,
            ),
            clock=FixedClock(),
            workflow=workflow,
            transcript_store=persistence,
            model_catalog=MODEL_CATALOG,
            shutdown_grace_period=0,
        )
        async with application.router.lifespan_context(application):
            coordinator = application.state.run_coordinator
            created = await coordinator.create_run(create_request_bytes("m9 stream"))
            await workflow.calls.get()
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.WATERBODY,
                target_comid="1234567",
            )
            await coordinator.mark_agent_session_running(
                created.run_id,
                session.session_id,
            )
            session_items = await coordinator.list_session_transcript_items(
                created.run_id,
                session.session_id,
            )
            operation = await coordinator.create_operation(
                    created.run_id,
                    session.session_id,
                )
            request = _model_request(
                operation.operation_id,
                session_items[0].item_id,
            )
            store = ReasoningTranscriptStore(
                operations=coordinator,
                run_id=created.run_id,
                session_id=session.session_id,
                private_values=("super-secret",),
            )
            response = await consume_waterbody_model_stream(
                store.recording_port(StreamingModelPort()),
                request,
                cancellation=NeverCancelled(),
            )
            await store.write_planning_decision(
                request,
                decision_type=ReasoningDecisionType.EVIDENCE_READY,
                bundle_ref=request.evidence_bundle_ref,
                rationale="The bounded evidence is ready.",
            )

            assert response.usage.input_tokens == 21
            runtime = coordinator._runs[created.run_id]
            process = runtime.agent_sessions[session.session_id].process_events
            names = tuple(
                "transcript_item"
                if isinstance(item, TranscriptItemProcessRecord)
                else "message_started"
                if isinstance(item, MessageStartedProcessRecord)
                else "message_delta"
                if isinstance(item, MessageDeltaProcessRecord)
                else "message_finalized"
                if isinstance(item, MessageFinalizedProcessRecord)
                else "unknown"
                for item in process
            )
            assert names.count("message_started") == 2
            assert names.count("message_finalized") == 2
            assert names.index("message_started") < names.index("message_delta")

            transcript = await application.state.transcript_reader.get_session_transcript(
                created.run_id,
                session.session_id,
                ReadView.AUTO,
            )
            types = tuple(item.content_type for item in transcript.items)
            assert types[-5:] == (
                "waterbody_model_request",
                "waterbody_agent_message",
                "waterbody_agent_message",
                "waterbody_model_response",
                "reasoning_decision",
            )
            serialized = json.dumps(
                [item.model_dump(mode="json") for item in transcript.items]
            )
            assert "super-secret" not in serialized
            response_item = next(
                item
                for item in transcript.items
                if item.content_type == "waterbody_model_response"
            )
            public_body = response_item.content.body
            assert public_body["structured_outputs"] == [
                {"output_id": "output-one", "present": True}
            ]
            assert "value" not in json.dumps(public_body)
            request_item = next(
                item
                for item in transcript.items
                if item.content_type == "waterbody_model_request"
            )
            assert request_item.parse_status.value == "ok"

            full = await application.state.transcript_reader.get_content(
                created.run_id,
                response_item.item_id,
                ReadView.AUTO,
            )
            assert "evidence_ready" in full.text
            assert "super-secret" not in full.text

            await coordinator.cancel_agent_session(
                created.run_id,
                session.session_id,
            )
            workflow.release.set()

    asyncio.run(exercise())
def test_result_and_waterbody_completion_share_one_persistence_group_and_history() -> None:
    async def exercise() -> None:
        model_port = AdaptiveWaterbodyModelPort()
        _scope, _evidence_store, loop, task = await _fixture(
            model_port,
            water_mode="full",
        )
        outcome = await _runner(loop, model_port).run(
            task,
            deadline=FixedClock().now() + timedelta(minutes=5),
        )
        assert outcome.result is not None

        workflow = HoldingWorkflow()
        persistence = InMemoryTranscriptStore()
        application = create_test_app(
            settings=AppSettings(_env_file=None),
            clock=FixedClock(),
            workflow=workflow,
            transcript_store=persistence,
            model_catalog=MODEL_CATALOG,
            shutdown_grace_period=0,
        )
        async with application.router.lifespan_context(application):
            coordinator = application.state.run_coordinator
            created = await coordinator.create_run(create_request_bytes("m9 result"))
            await workflow.calls.get()
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.WATERBODY,
                target_comid="1234567",
            )
            await coordinator.mark_agent_session_running(
                created.run_id,
                session.session_id,
            )
            result = outcome.result.model_copy(update={
                "run_id": created.run_id,
                "session_id": session.session_id,
                "target_comid": "1234567",
                "analysis_detail": outcome.result.analysis_detail.model_copy(
                    update={"transcript_item_refs": ()}
                ),
            })
            completed = await coordinator.complete_waterbody_agent_session(
                created.run_id,
                session.session_id,
                result,
            )
            assert completed.status is AgentSessionStatus.COMPLETED
            assert completed.result_summary.result_id == result.result_id
            await _wait_persisted(coordinator, created.run_id)

            terminal_batch = next(
                batch
                for batch in reversed(persistence.write_batches)
                if any(
                    mutation.summary.status is AgentSessionStatus.COMPLETED
                    for mutation in batch.session_mutations
                )
            )
            item_types = {
                item.item_id: item.content_type
                for item in terminal_batch.transcript.items
            }
            terminal_group = next(
                group
                for group in terminal_batch.groups
                if {
                    item_types[item_id] for item_id in group.item_ids
                } == {"inference_result", "lifecycle_event"}
            )
            assert tuple(item_types[item_id] for item_id in terminal_group.item_ids) == (
                "inference_result",
                "lifecycle_event",
            )
            assert len(terminal_group.mutation_ids) == 1

            history = await application.state.transcript_reader.get_session(
                created.run_id,
                session.session_id,
                ReadView.HISTORY,
            )
            assert history.session.result_ref == completed.result_ref
            assert history.session.result_summary == completed.result_summary
            run = await application.state.transcript_reader.get_run(
                created.run_id,
                ReadView.HISTORY,
            )
            summary_text = json.dumps(run.sessions[0].model_dump(mode="json"))
            assert result.result_id in summary_text
            assert "series" not in summary_text
            assert "risk_rationale" not in summary_text

            transcript = await application.state.transcript_reader.get_session_transcript(
                created.run_id,
                session.session_id,
                ReadView.HISTORY,
            )
            assert tuple(item.content_type for item in transcript.items[-2:]) == (
                "inference_result",
                "lifecycle_event",
            )

            cancelled_session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.WATERBODY,
                target_comid="7654321",
            )
            await coordinator.mark_agent_session_running(
                created.run_id,
                cancelled_session.session_id,
            )
            await coordinator.cancel_agent_session(
                created.run_id,
                cancelled_session.session_id,
            )
            cancelled = await coordinator.complete_waterbody_agent_session(
                created.run_id,
                cancelled_session.session_id,
                result.model_copy(update={
                    "session_id": cancelled_session.session_id,
                    "target_comid": "7654321",
                }),
            )
            assert cancelled.status is AgentSessionStatus.CANCELLED
            assert cancelled.result_ref is None
            cancelled_transcript = (
                await application.state.transcript_reader.get_session_transcript(
                    created.run_id,
                    cancelled_session.session_id,
                    ReadView.AUTO,
                )
            )
            assert all(
                item.content_type != "inference_result"
                for item in cancelled_transcript.items
            )
            workflow.release.set()

    asyncio.run(exercise())
