import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.api.schemas.analysis_runs import CreateAnalysisRunRequest
from app.application.analysis_runs.startup_repair import StartupRepairError
from app.application.analysis_runs.models import AgentRole, AgentSessionStatus, RunStatus
from app.application.analysis_runs.coordinator import RunNotFoundError
from app.application.analysis_runs.workflow import WorkflowContext
from app.application.transcripts.models import (
    FixtureMessageV1,
    GenerationStatus,
    LateMessageOutputFact,
    MessageEndReason,
)
from app.application.transcripts.registry import (
    create_production_registry,
    fixture_message_registration,
)
from app.infrastructure.config import AppSettings
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from app.main import create_app


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

    async def run(self, context: WorkflowContext) -> None:
        self.started.set()
        await self.release.wait()
        context.cancellation.checkpoint()


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
            message = await coordinator.start_transcript_message(
                created.run_id,
                session.session_id,
                "fixture_message",
                1,
                private_values=("secret-value",),
            )
            await coordinator.append_transcript_message_delta(
                created.run_id,
                message.item_id,
                "kept ",
            )
            await coordinator.cancel_run(created.run_id)
            assert await coordinator.append_transcript_message_delta(
                created.run_id,
                message.item_id,
                "late secret-",
            ) == ""
            assert await coordinator.append_transcript_message_delta(
                created.run_id,
                message.item_id,
                "value tail",
            ) == ""
            await coordinator.finalize_transcript_message(
                created.run_id,
                message.item_id,
                FixtureMessageV1(
                    text="kept late secret-value tail",
                    generation_status=GenerationStatus.COMPLETED,
                    end_reason=MessageEndReason.COMPLETED,
                ),
            )
            workflow.release.set()
            await coordinator._runs[created.run_id].workflow_quiesced.wait()
            await wait_saved(application, created.run_id)

            stored = await store.read_run(created.run_id)
            assert stored is not None
            bodies = {
                item.item_id: item.inline_body
                for item in stored.items
                if item.inline_body is not None
            }
            original = json.loads(bodies[message.item_id])
            assert original == {
                "text": "kept ",
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
            assert late.text == "late [REDACTED] tail"
            assert "secret-value" not in "".join(bodies.values())

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
