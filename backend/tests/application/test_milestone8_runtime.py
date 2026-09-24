import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.api.schemas.analysis_runs import CreateAnalysisRunRequest
from app.application.analysis_runs.startup_repair import StartupRepairError
from app.application.analysis_runs.models import AgentRole, AgentSessionStatus, RunStatus
from app.application.analysis_runs.coordinator import (
    InvalidAgentSessionTransitionError,
    InvalidRunTransitionError,
    RunNotFoundError,
)
from app.application.analysis_runs.persistence import PersistenceState
from app.application.analysis_runs.workflow import WorkflowContext
from app.application.transcripts.models import (
    GenerationStatus,
    LateMessageOutputFact,
    MessageEndReason,
)
from app.application.transcripts.registry import create_production_registry
from app.application.transcripts.stream_models import (
    MessageFinalizedProcessRecord,
    TranscriptItemProcessRecord,
)
from app.infrastructure.config import AppSettings
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from app.main import create_app
from scripts.transcript_fixtures import (
    FixtureMessageV1,
    fixture_message_registration,
)


CONFIGURATION = CreateAnalysisRunRequest(query="query").to_configuration_snapshot(
    "America/Chicago"
)


class TickingClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 14, tzinfo=UTC)

    def now(self):
        value = self.value
        self.value += timedelta(microseconds=1)
        return value


class GatedWorkflow:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.error: Exception | None = None

    async def run(self, context: WorkflowContext) -> None:
        self.started.set()
        await self.release.wait()
        context.cancellation.checkpoint()
        if self.error is not None:
            raise self.error


async def wait_saved(application, run_id) -> None:
    for _ in range(200):
        state = await application.state.run_coordinator.get_persistence_info(run_id)
        if state.state.value == "up_to_date":
            return
        await asyncio.sleep(0)
    raise AssertionError("persistence writer did not settle")


def run(exercise) -> None:
    asyncio.run(asyncio.wait_for(exercise(), timeout=10))


def test_cancel_freezes_original_and_saves_only_late_tail_as_history_audit() -> None:
    async def exercise() -> None:
        workflow = GatedWorkflow()
        store = InMemoryTranscriptStore()
        registry = create_production_registry().copy_with(
            fixture_message_registration()
        )
        application = create_app(
            AppSettings(_env_file=None),
            clock=TickingClock(),
            workflow=workflow,
            transcript_store=store,
            transcript_registry=registry,
            shutdown_grace_period=0,
        )
        async with application.router.lifespan_context(application):
            coordinator = application.state.run_coordinator
            created = await coordinator.create_run("cancel boundary", CONFIGURATION)
            await workflow.started.wait()
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            await coordinator.mark_agent_session_running(
                created.run_id,
                session.session_id,
            )
            generic = await coordinator.reserve_transcript_item(created.run_id)
            message = await coordinator.start_transcript_message(
                created.run_id,
                session.session_id,
                "fixture_message",
                1,
            )
            await coordinator.append_transcript_message_delta(
                created.run_id,
                message.item_id,
                "kept api_key=",
            )
            await coordinator.cancel_run(created.run_id)
            process = coordinator._runs[created.run_id].agent_sessions[
                session.session_id
            ].process_events
            assert isinstance(process[-2], MessageFinalizedProcessRecord)
            assert isinstance(process[-1], TranscriptItemProcessRecord)
            assert "session_cancelled" in process[-1].item.content.safe_body
            with pytest.raises(InvalidRunTransitionError):
                await coordinator.reserve_transcript_item(created.run_id)
            assert await coordinator.finalize_transcript_item(
                created.run_id,
                generic.item_id,
                "analysis_request",
                1,
                {"query": "suppressed late result"},
            ) is generic
            assert generic not in await coordinator.list_transcript_items(
                created.run_id
            )
            assert await coordinator.append_transcript_message_delta(
                created.run_id,
                message.item_id,
                "abcdef",
            ) == ""
            late_chunk = await coordinator.append_transcript_message_delta(
                created.run_id,
                message.item_id,
                " tail",
            )
            assert "abcdef" not in late_chunk
            await coordinator.finalize_transcript_message(
                created.run_id,
                message.item_id,
                FixtureMessageV1(
                    text="kept api_key=abcdef tail",
                    generation_status=GenerationStatus.COMPLETED,
                    end_reason=MessageEndReason.COMPLETED,
                ),
            )
            workflow.release.set()
            await coordinator._runs[created.run_id].workflow_quiesced.wait()
            await wait_saved(application, created.run_id)

            stored = await store.read_run(created.run_id)
            assert stored is not None
            assert all(item.item_id != generic.item_id for item in stored.items)
            bodies = {
                item.item_id: item.inline_body
                for item in stored.items
                if item.inline_body is not None
            }
            original = json.loads(bodies[message.item_id])
            assert original == {
                "text": "kept api_key=[REDACTED]",
                "generation_status": "incomplete",
                "end_reason": "cancelled",
            }
            audits = [
                registry.parse(item, bodies[item.item_id]).body
                for item in stored.items
                if item.content_type == "runtime_fact"
                and item.item_id in bodies
            ]
            late = next(item for item in audits if isinstance(item, LateMessageOutputFact))
            assert late.original_item_id == message.item_id
            assert late.text == " tail"
            assert "abcdef" not in "".join(bodies.values())

    run(exercise)


def test_cancelled_short_marker_prefix_masks_the_late_credential() -> None:
    async def exercise() -> None:
        workflow = GatedWorkflow()
        store = InMemoryTranscriptStore()
        registry = create_production_registry().copy_with(
            fixture_message_registration()
        )
        application = create_app(
            AppSettings(_env_file=None),
            clock=TickingClock(),
            workflow=workflow,
            transcript_store=store,
            transcript_registry=registry,
            shutdown_grace_period=0,
        )
        async with application.router.lifespan_context(application):
            coordinator = application.state.run_coordinator
            created = await coordinator.create_run("short cancel boundary", CONFIGURATION)
            await workflow.started.wait()
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            await coordinator.mark_agent_session_running(
                created.run_id,
                session.session_id,
            )
            message = await coordinator.start_transcript_message(
                created.run_id,
                session.session_id,
                "fixture_message",
                1,
            )
            await coordinator.append_transcript_message_delta(
                created.run_id,
                message.item_id,
                "safe api",
            )
            await coordinator.cancel_run(created.run_id)
            await coordinator.append_transcript_message_delta(
                created.run_id,
                message.item_id,
                "_key=alphabetic tail",
            )
            await coordinator.finalize_transcript_message(
                created.run_id,
                message.item_id,
                FixtureMessageV1(
                    text="safe api_key=alphabetic tail",
                    generation_status=GenerationStatus.COMPLETED,
                ),
            )
            workflow.release.set()
            await coordinator._runs[created.run_id].workflow_quiesced.wait()
            await wait_saved(application, created.run_id)

            stored = await store.read_run(created.run_id)
            bodies = {
                item.item_id: item.inline_body
                for item in stored.items
                if item.inline_body is not None
            }
            original = json.loads(bodies[message.item_id])
            assert original["text"] == "safe [REDACTED]"
            late = next(
                registry.parse(item, bodies[item.item_id]).body
                for item in stored.items
                if item.content_type == "runtime_fact"
                and item.item_id in bodies
                and "late_message_output" in bodies[item.item_id]
            )
            assert late.text == "_key=[REDACTED] tail"
            assert "alphabetic" not in "".join(bodies.values())

    run(exercise)


@pytest.mark.parametrize("workflow_error", [False, True])
def test_workflow_failure_closes_sessions_messages_and_pending_results(
    workflow_error,
) -> None:
    async def exercise() -> None:
        workflow = GatedWorkflow()
        store = InMemoryTranscriptStore()
        registry = create_production_registry().copy_with(
            fixture_message_registration()
        )
        application = create_app(
            AppSettings(_env_file=None),
            clock=TickingClock(),
            workflow=workflow,
            transcript_store=store,
            transcript_registry=registry,
            shutdown_grace_period=0,
        )
        async with application.router.lifespan_context(application):
            coordinator = application.state.run_coordinator
            created = await coordinator.create_run("failed workflow", CONFIGURATION)
            await workflow.started.wait()
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            pending_session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.WATERBODY,
                target_comid="456",
            )
            await coordinator.mark_agent_session_running(
                created.run_id,
                session.session_id,
            )
            message = await coordinator.start_transcript_message(
                created.run_id,
                session.session_id,
                "fixture_message",
                1,
            )
            await coordinator.append_transcript_message_delta(
                created.run_id,
                message.item_id,
                "partial answer",
            )
            generic = await coordinator.reserve_transcript_item(
                created.run_id,
                session_id=session.session_id,
            )
            if workflow_error:
                workflow.error = RuntimeError("injected workflow failure")
            workflow.release.set()

            runtime = coordinator._runs[created.run_id]
            await runtime.workflow_quiesced.wait()
            await wait_saved(application, created.run_id)

            assert runtime.snapshot.status is RunStatus.FAILED
            assert runtime.agent_sessions[
                session.session_id
            ].summary.status is AgentSessionStatus.FAILED
            assert runtime.agent_sessions[
                pending_session.session_id
            ].summary.status is AgentSessionStatus.FAILED
            failure_ids = {
                runtime.last_error.error_id,
                runtime.agent_sessions[session.session_id].last_error.error_id,
                runtime.agent_sessions[
                    pending_session.session_id
                ].last_error.error_id,
            }
            assert len(failure_ids) == 3
            assert message.item_id not in runtime.streaming_messages
            assert message.item_id not in runtime.late_streaming_messages
            assert generic.item_id in runtime.suppressed_transcript_items
            assert await coordinator.finalize_transcript_item(
                created.run_id,
                generic.item_id,
                "analysis_request",
                1,
                {"query": "late generic result"},
            ) is generic

            process = runtime.agent_sessions[session.session_id].process_events
            finalized_index = next(
                index
                for index, record in enumerate(process)
                if isinstance(record, MessageFinalizedProcessRecord)
            )
            terminal_index = next(
                index
                for index, record in enumerate(process)
                if isinstance(record, TranscriptItemProcessRecord)
                and "session_failed" in record.item.content.safe_body
            )
            assert finalized_index < terminal_index
            message_body = next(
                item.content.safe_body
                for item in runtime.transcript_buffer.items
                if item.item_id == message.item_id
            )
            assert json.loads(message_body) == {
                "text": "partial answer",
                "generation_status": "incomplete",
                "end_reason": "error",
            }

            assert len([
                event async for event in await coordinator.subscribe_session(
                    created.run_id,
                    session.session_id,
                )
            ]) == len(process)
            assert len([
                event async for event in await coordinator.subscribe_run(
                    created.run_id
                )
            ]) == 1

            stored = await store.read_run(created.run_id)
            assert stored is not None
            assert all(item.item_id != generic.item_id for item in stored.items)
            assert next(
                item for item in stored.items if item.item_id == message.item_id
            ).inline_body == message_body

            coordinator._live_state_grace_period = 0
            async with runtime.condition:
                runtime.condition.notify_all()
            for _ in range(200):
                if created.run_id not in coordinator._runs:
                    break
                await asyncio.sleep(0)
            assert created.run_id not in coordinator._runs

    run(exercise)


def test_single_session_cancel_contains_stream_and_generic_late_results() -> None:
    async def exercise() -> None:
        workflow = GatedWorkflow()
        store = InMemoryTranscriptStore()
        registry = create_production_registry().copy_with(
            fixture_message_registration()
        )
        application = create_app(
            AppSettings(_env_file=None),
            clock=TickingClock(),
            workflow=workflow,
            transcript_store=store,
            transcript_registry=registry,
            shutdown_grace_period=0,
        )
        async with application.router.lifespan_context(application):
            coordinator = application.state.run_coordinator
            created = await coordinator.create_run("session cancel", CONFIGURATION)
            await workflow.started.wait()
            cancelled_session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            other_session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.WATERBODY,
                target_comid="123",
            )
            for session in (cancelled_session, other_session):
                await coordinator.mark_agent_session_running(
                    created.run_id,
                    session.session_id,
                )
            cancelled_message = await coordinator.start_transcript_message(
                created.run_id,
                cancelled_session.session_id,
                "fixture_message",
                1,
            )
            other_message = await coordinator.start_transcript_message(
                created.run_id,
                other_session.session_id,
                "fixture_message",
                1,
            )
            await coordinator.append_transcript_message_delta(
                created.run_id,
                cancelled_message.item_id,
                "kept",
            )
            await coordinator.append_transcript_message_delta(
                created.run_id,
                other_message.item_id,
                "other",
            )
            generic = await coordinator.reserve_transcript_item(
                created.run_id,
                session_id=cancelled_session.session_id,
            )

            cancelled = await coordinator.cancel_agent_session(
                created.run_id,
                cancelled_session.session_id,
            )
            runtime = coordinator._runs[created.run_id]
            assert cancelled.status is AgentSessionStatus.CANCELLED
            assert runtime.agent_sessions[
                other_session.session_id
            ].summary.status is AgentSessionStatus.RUNNING
            assert set(runtime.streaming_messages) == {other_message.item_id}
            assert set(runtime.late_streaming_messages) == {
                cancelled_message.item_id
            }
            process = runtime.agent_sessions[
                cancelled_session.session_id
            ].process_events
            assert isinstance(process[-2], MessageFinalizedProcessRecord)
            assert isinstance(process[-1], TranscriptItemProcessRecord)
            assert "session_cancelled" in process[-1].item.content.safe_body
            with pytest.raises(InvalidAgentSessionTransitionError):
                await coordinator.reserve_transcript_item(
                    created.run_id,
                    session_id=cancelled_session.session_id,
                )

            assert await coordinator.append_transcript_message_delta(
                created.run_id,
                cancelled_message.item_id,
                " late tail",
            ) == ""
            await coordinator.finalize_transcript_message(
                created.run_id,
                cancelled_message.item_id,
                FixtureMessageV1(
                    text="kept late tail",
                    generation_status=GenerationStatus.COMPLETED,
                ),
            )
            assert await coordinator.finalize_transcript_item(
                created.run_id,
                generic.item_id,
                "analysis_request",
                1,
                {"query": "suppressed"},
            ) is generic
            await coordinator.finalize_transcript_message(
                created.run_id,
                other_message.item_id,
                FixtureMessageV1(
                    text="other",
                    generation_status=GenerationStatus.COMPLETED,
                ),
            )
            await coordinator.complete_agent_session(
                created.run_id,
                other_session.session_id,
            )
            workflow.release.set()
            await runtime.workflow_quiesced.wait()
            await wait_saved(application, created.run_id)

            assert runtime.snapshot.status is RunStatus.COMPLETED
            assert generic.item_id in runtime.suppressed_transcript_items
            assert not runtime.streaming_messages
            assert not runtime.late_streaming_messages
            stored = await store.read_run(created.run_id)
            assert stored is not None
            assert all(item.item_id != generic.item_id for item in stored.items)
            audit = next(
                item
                for item in stored.items
                if item.content_type == "runtime_fact"
                and item.inline_body is not None
                and "late_message_output" in item.inline_body
            )
            parsed_audit = registry.parse(audit, audit.inline_body)
            assert isinstance(parsed_audit.body, LateMessageOutputFact)
            assert parsed_audit.body.original_item_id == cancelled_message.item_id
            assert parsed_audit.body.text == " late tail"
            assert parsed_audit.body.end_reason is MessageEndReason.CANCELLED

    run(exercise)


def test_next_lifespan_repairs_interrupted_run_and_session_once() -> None:
    async def exercise() -> None:
        store = InMemoryTranscriptStore()
        workflow = GatedWorkflow()
        settings = AppSettings(_env_file=None)
        first = create_app(
            settings,
            clock=TickingClock(),
            workflow=workflow,
            transcript_store=store,
            shutdown_grace_period=0,
        )
        async with first.router.lifespan_context(first):
            coordinator = first.state.run_coordinator
            created = await coordinator.create_run("interrupted", CONFIGURATION)
            await workflow.started.wait()
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            await coordinator.mark_agent_session_running(
                created.run_id,
                session.session_id,
            )
            await wait_saved(first, created.run_id)

        batches_before = len(store.write_batches)
        second = create_app(settings, transcript_store=store, clock=TickingClock())
        async with second.router.lifespan_context(second):
            assert second.state.run_coordinator._runs == {}
            repaired = await store.read_run(created.run_id)
            assert repaired is not None
            assert repaired.run.status is RunStatus.FAILED
            assert repaired.sessions[0].status is AgentSessionStatus.FAILED
            failure_bodies = [
                item.inline_body
                for item in repaired.items
                if item.content_type == "lifecycle_event"
                and item.inline_body is not None
                and "service_interrupted" in item.inline_body
            ]
            assert len(failure_bodies) == 2
        assert len(store.write_batches) == batches_before + 1

        third = create_app(settings, transcript_store=store, clock=TickingClock())
        async with third.router.lifespan_context(third):
            pass
        assert len(store.write_batches) == batches_before + 1

    run(exercise)


@pytest.mark.parametrize("failure", ("unsupported", "corrupt"))
def test_terminal_run_session_repair_ignores_unreadable_run_creation(
    failure: str,
) -> None:
    async def exercise() -> None:
        store = InMemoryTranscriptStore()
        workflow = GatedWorkflow()
        settings = AppSettings(_env_file=None)
        first = create_app(
            settings,
            clock=TickingClock(),
            workflow=workflow,
            transcript_store=store,
            shutdown_grace_period=0,
        )
        async with first.router.lifespan_context(first):
            coordinator = first.state.run_coordinator
            created = await coordinator.create_run("terminal parent", CONFIGURATION)
            await workflow.started.wait()
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            await coordinator.mark_agent_session_running(
                created.run_id,
                session.session_id,
            )
            await coordinator.complete_run(created.run_id)
            await wait_saved(first, created.run_id)

        stored = await store.read_run(created.run_id)
        assert stored is not None
        assert stored.run.status is RunStatus.COMPLETED
        assert stored.sessions[0].status is AgentSessionStatus.RUNNING
        creation = store._items[stored.run.created_item_id]
        update = (
            {"content_schema_version": 999}
            if failure == "unsupported"
            else {"inline_body": "x" * creation.content_bytes}
        )
        store._items[stored.run.created_item_id] = creation.model_copy(
            update=update
        )
        batches_before = len(store.write_batches)

        second = create_app(settings, transcript_store=store, clock=TickingClock())
        async with second.router.lifespan_context(second):
            repaired = await store.read_run(created.run_id)
            assert repaired is not None
            assert repaired.run.status is RunStatus.COMPLETED
            assert repaired.sessions[0].status is AgentSessionStatus.FAILED
            failure_bodies = [
                item.inline_body
                for item in repaired.items
                if item.session_id == session.session_id
                and item.inline_body is not None
                and "service_interrupted" in item.inline_body
            ]
            assert len(failure_bodies) == 1
        assert len(store.write_batches) == batches_before + 1

    run(exercise)


def test_startup_repair_keeps_prior_commits_and_retries_remaining_run() -> None:
    class QueuedWorkflow:
        def __init__(self) -> None:
            self.started: asyncio.Queue[None] = asyncio.Queue()
            self.release = asyncio.Event()

        async def run(self, context: WorkflowContext) -> None:
            self.started.put_nowait(None)
            await self.release.wait()
            context.cancellation.checkpoint()

    class FailingRepairStore(InMemoryTranscriptStore):
        def __init__(self) -> None:
            super().__init__()
            self.fail_repair_call: int | None = None
            self.repair_calls = 0

        async def write_batch(self, batch) -> None:
            is_repair = any(
                mutation.snapshot.status is RunStatus.FAILED
                for mutation in batch.run_mutations
            )
            if is_repair:
                self.repair_calls += 1
                if self.repair_calls == self.fail_repair_call:
                    raise OSError("injected startup repair failure")
            await super().write_batch(batch)

    async def exercise() -> None:
        settings = AppSettings(_env_file=None)
        workflow = QueuedWorkflow()
        store = FailingRepairStore()
        first = create_app(
            settings,
            workflow=workflow,
            transcript_store=store,
            shutdown_grace_period=0,
        )
        async with first.router.lifespan_context(first):
            run_ids = []
            for query in ("first interrupted", "second interrupted"):
                created = await first.state.run_coordinator.create_run(
                    query,
                    CONFIGURATION,
                )
                run_ids.append(created.run_id)
                await workflow.started.get()
                await wait_saved(first, created.run_id)

        store.fail_repair_call = 2
        failed_start = create_app(settings, transcript_store=store)
        with pytest.raises(StartupRepairError):
            async with failed_start.router.lifespan_context(failed_start):
                raise AssertionError("startup must not accept traffic after repair failure")

        interrupted = [await store.read_run(run_id) for run_id in run_ids]
        assert all(item is not None for item in interrupted)
        assert sum(item.run.status is RunStatus.FAILED for item in interrupted) == 1
        assert sum(item.run.status is RunStatus.RUNNING for item in interrupted) == 1

        store.fail_repair_call = None
        repaired_start = create_app(settings, transcript_store=store)
        async with repaired_start.router.lifespan_context(repaired_start):
            pass
        repaired = [await store.read_run(run_id) for run_id in run_ids]
        assert all(item is not None for item in repaired)
        assert all(item.run.status is RunStatus.FAILED for item in repaired)
        repair_batches = len(store.write_batches)

        repeated_start = create_app(settings, transcript_store=store)
        async with repeated_start.router.lifespan_context(repeated_start):
            pass
        assert len(store.write_batches) == repair_batches

    run(exercise)


def test_live_cleanup_waits_for_terminal_quiescence_and_retries_failure() -> None:
    async def exercise() -> None:
        workflow = GatedWorkflow()
        store = InMemoryTranscriptStore()
        application = create_app(
            AppSettings(_env_file=None, live_state_grace_seconds=0.01),
            workflow=workflow,
            transcript_store=store,
        )
        async with application.router.lifespan_context(application):
            coordinator = application.state.run_coordinator
            created = await coordinator.create_run("cleanup", CONFIGURATION)
            await workflow.started.wait()
            assert created.run_id in coordinator._runs

            original_release = coordinator._release_live_state
            attempts = 0

            async def fail_once(runtime):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("injected cleanup failure")
                return await original_release(runtime)

            coordinator._release_live_state = fail_once
            workflow.release.set()
            for _ in range(500):
                if created.run_id not in coordinator._runs:
                    break
                await asyncio.sleep(0.001)
            assert attempts >= 2
            assert created.run_id not in coordinator._runs
            assert await store.read_run(created.run_id) is not None
            try:
                await coordinator.get_snapshot(created.run_id)
            except RunNotFoundError:
                pass
            else:
                raise AssertionError("cleaned live state remained addressable")

    run(exercise)


@pytest.mark.parametrize("first", ("retry", "cleanup"))
def test_persistence_retry_and_cleanup_have_one_linearization_point(
    first: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        workflow = GatedWorkflow()
        store = InMemoryTranscriptStore()
        application = create_app(
            AppSettings(_env_file=None, live_state_grace_seconds=3600),
            workflow=workflow,
            transcript_store=store,
            shutdown_grace_period=0,
        )
        async with application.router.lifespan_context(application):
            coordinator = application.state.run_coordinator
            created = await coordinator.create_run("cleanup race", CONFIGURATION)
            await workflow.started.wait()
            runtime = coordinator._runs[created.run_id]
            workflow.release.set()
            await runtime.workflow_quiesced.wait()
            await wait_saved(application, created.run_id)

            writer = runtime.writer_task
            assert runtime.cleanup_task is not None
            runtime.cleanup_task.cancel()
            await asyncio.gather(runtime.cleanup_task, return_exceptions=True)
            runtime.cleanup_eligible_at = (
                asyncio.get_running_loop().time()
                - coordinator._live_state_grace_period
                - 1
            )

            original_acquire = runtime.condition.acquire
            first_entered = asyncio.Event()
            allow_first = asyncio.Event()

            async def gated_acquire() -> bool:
                if asyncio.current_task().get_name() == first:
                    first_entered.set()
                    await allow_first.wait()
                return await original_acquire()

            monkeypatch.setattr(runtime.condition, "acquire", gated_acquire)
            first_call = (
                coordinator.retry_persistence(created.run_id)
                if first == "retry"
                else coordinator._release_live_state(runtime)
            )
            first_task = asyncio.create_task(first_call, name=first)
            await first_entered.wait()
            lock_held_at_condition = coordinator._runs_lock.locked()
            second = "cleanup" if first == "retry" else "retry"
            second_call = (
                coordinator._release_live_state(runtime)
                if second == "cleanup"
                else coordinator.retry_persistence(created.run_id)
            )
            second_task = asyncio.create_task(second_call, name=second)
            allow_first.set()

            if first == "retry":
                retry_result, cleanup_result = await asyncio.gather(
                    first_task,
                    second_task,
                )
            else:
                cleanup_result, retry_result = await asyncio.gather(
                    first_task,
                    second_task,
                )

            assert lock_held_at_condition
            assert cleanup_result is True
            assert runtime.writer_task is writer
            if first == "retry":
                assert retry_result is not None
                assert retry_result.state is PersistenceState.UP_TO_DATE
            else:
                assert retry_result is None
            assert created.run_id not in coordinator._runs
            recovery_facts = [
                item
                for item in runtime.transcript_buffer.items
                if item.content.content_type == "runtime_fact"
                and json.loads(item.content.safe_body)["fact"]
                == "persistence_recovered"
            ]
            assert recovery_facts == []

    run(exercise)
