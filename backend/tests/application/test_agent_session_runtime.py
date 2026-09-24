import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from app.api.schemas.analysis_runs import CreateAnalysisRunRequest
from app.application.analysis_runs.coordinator import (
    AgentSessionNotFoundError,
    InvalidAgentSessionTransitionError,
    InvalidRunTransitionError,
    RunCoordinator,
)
from app.application.analysis_runs.models import (
    AgentRole,
    AgentSessionStatus,
    PublicError,
    RunStateMutation,
    RunStatus,
    SessionStateMutation,
)
from app.application.analysis_runs.workflow import WorkflowContext
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore

CONFIGURATION = CreateAnalysisRunRequest(query="query").to_configuration_snapshot(
    "America/Chicago"
)
ERROR = PublicError(
    error_id=UUID("0463cd90-6d0f-47e9-9656-b4aa86352c61"),
    code="agent_failed",
    message="The Agent failed.",
    failed_stage="agent_execution",
    retryable=False,
)


class StepClock:
    def __init__(self) -> None:
        self.calls = 0
        self.instant = datetime(2026, 9, 8, 10, tzinfo=timezone.utc)

    def now(self) -> datetime:
        self.calls += 1
        return self.instant + timedelta(microseconds=self.calls)


class SequentialIds:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> UUID:
        self.value += 1
        return UUID(f"00000000-0000-4000-8000-{self.value:012x}")


class HoldingWorkflow:
    def __init__(self) -> None:
        self.started: dict[UUID, asyncio.Event] = {}

    async def run(self, context: WorkflowContext) -> None:
        self.started.setdefault(context.run.run_id, asyncio.Event()).set()
        await context.cancellation.wait()
        context.cancellation.checkpoint()


async def make_running_coordinator():
    clock = StepClock()
    workflow = HoldingWorkflow()
    coordinator = RunCoordinator(
        clock,
        transcript_store=InMemoryTranscriptStore(),
        workflow=workflow,
        record_id_factory=SequentialIds(),
    )
    run = await coordinator.create_run("query", CONFIGURATION)
    await workflow.started.setdefault(run.run_id, asyncio.Event()).wait()
    assert (await coordinator.get_snapshot(run.run_id)).status is RunStatus.RUNNING
    return coordinator, clock, run.run_id


def run_test(exercise) -> None:
    async def managed() -> None:
        coordinator, clock, run_id = await make_running_coordinator()
        try:
            await exercise(coordinator, clock, run_id)
        finally:
            await coordinator.shutdown()

    asyncio.run(asyncio.wait_for(managed(), timeout=5))


def test_sessions_are_created_only_at_prepare_boundary_and_deduplicated() -> None:
    async def exercise(coordinator, clock, run_id) -> None:
        assert await coordinator.list_sessions(run_id) == ()

        parser_results = await asyncio.gather(
            *(
                coordinator.prepare_agent_session(
                    run_id,
                    agent_role=AgentRole.QUERY_PARSER,
                )
                for _ in range(8)
            )
        )
        waterbody_results = await asyncio.gather(
            *(
                coordinator.prepare_agent_session(
                    run_id,
                    agent_role=AgentRole.WATERBODY,
                    target_comid=" 001234 ",
                )
                for _ in range(8)
            )
        )
        other = await coordinator.prepare_agent_session(
            run_id,
            agent_role=AgentRole.WATERBODY,
            target_comid="009999",
        )

        assert all(item is parser_results[0] for item in parser_results)
        assert all(item is waterbody_results[0] for item in waterbody_results)
        assert waterbody_results[0].target_comid == "001234"
        assert len({parser_results[0].session_id, waterbody_results[0].session_id, other.session_id}) == 3
        assert await coordinator.list_sessions(run_id) == (
            parser_results[0],
            waterbody_results[0],
            other,
        )

    run_test(exercise)


def test_waterbody_session_is_created_and_claimed_once_after_all_upstream_gates() -> None:
    async def exercise(coordinator, clock, run_id) -> None:
        upstreams = [asyncio.Event() for _ in range(3)]

        async def prepare_after_upstreams():
            await asyncio.gather(*(upstream.wait() for upstream in upstreams))
            session = await coordinator.prepare_agent_session(
                run_id,
                agent_role=AgentRole.WATERBODY,
                target_comid="001234",
            )
            try:
                running = await coordinator.mark_agent_session_running(
                    run_id,
                    session.session_id,
                )
            except InvalidAgentSessionTransitionError:
                return session.session_id, False
            return running.session_id, True

        contenders = [asyncio.create_task(prepare_after_upstreams()) for _ in range(8)]
        upstreams[0].set()
        upstreams[1].set()
        await asyncio.sleep(0)
        assert await coordinator.list_sessions(run_id) == ()

        upstreams[2].set()
        results = await asyncio.gather(*contenders)
        sessions = await coordinator.list_sessions(run_id)

        assert len({session_id for session_id, _ in results}) == 1
        assert sum(claimed for _, claimed in results) == 1
        assert len(sessions) == 1
        assert sessions[0].status is AgentSessionStatus.RUNNING
        assert coordinator._runs[run_id].agent_sessions[
            sessions[0].session_id
        ].execution_claimed

    run_test(exercise)


@pytest.mark.parametrize(
    ("role", "comid"),
    [
        (AgentRole.QUERY_PARSER, "001234"),
        (AgentRole.WATERBODY, None),
        (AgentRole.WATERBODY, "  "),
    ],
)
def test_prepare_rejects_invalid_role_comid_without_creating_context(role, comid) -> None:
    async def exercise(coordinator, clock, run_id) -> None:
        before = clock.calls
        with pytest.raises(ValueError):
            await coordinator.prepare_agent_session(
                run_id,
                agent_role=role,
                target_comid=comid,
            )
        assert await coordinator.list_sessions(run_id) == ()
        assert clock.calls == before

    run_test(exercise)


def test_session_running_is_claimed_once_and_terminal_is_immutable() -> None:
    async def exercise(coordinator, clock, run_id) -> None:
        session = await coordinator.prepare_agent_session(
            run_id,
            agent_role=AgentRole.QUERY_PARSER,
        )
        results = await asyncio.gather(
            coordinator.mark_agent_session_running(run_id, session.session_id),
            coordinator.mark_agent_session_running(run_id, session.session_id),
            return_exceptions=True,
        )
        running = next(item for item in results if not isinstance(item, Exception))
        rejected = next(item for item in results if isinstance(item, Exception))

        assert running.status is AgentSessionStatus.RUNNING
        assert isinstance(rejected, InvalidAgentSessionTransitionError)
        completed = await coordinator.complete_agent_session(run_id, session.session_id)
        with pytest.raises(InvalidAgentSessionTransitionError):
            await coordinator.cancel_agent_session(run_id, session.session_id)
        assert await coordinator.get_session(run_id, session.session_id) is completed
        runtime = coordinator._runs[run_id].agent_sessions[session.session_id]
        assert runtime.execution_claimed is True
        assert [event.type.value for event in runtime.events] == [
            "session_created",
            "session_completed",
        ]

    run_test(exercise)


def test_session_can_fail_during_preparation() -> None:
    async def exercise(coordinator, clock, run_id) -> None:
        session = await coordinator.prepare_agent_session(
            run_id,
            agent_role=AgentRole.WATERBODY,
            target_comid="001234",
        )
        failed = await coordinator.fail_agent_session(
            run_id,
            session.session_id,
            ERROR,
        )

        assert failed.status is AgentSessionStatus.FAILED
        runtime = coordinator._runs[run_id].agent_sessions[session.session_id]
        assert runtime.execution_claimed is False
        assert runtime.last_error is ERROR
        assert runtime.events[-1].payload.error is ERROR

    run_test(exercise)


def test_run_cancel_preserves_completed_session_and_cancels_only_active_sessions() -> None:
    async def exercise(coordinator, clock, run_id) -> None:
        completed = await coordinator.prepare_agent_session(
            run_id,
            agent_role=AgentRole.QUERY_PARSER,
        )
        await coordinator.mark_agent_session_running(run_id, completed.session_id)
        completed = await coordinator.complete_agent_session(run_id, completed.session_id)
        running = await coordinator.prepare_agent_session(
            run_id,
            agent_role=AgentRole.WATERBODY,
            target_comid="001234",
        )
        await coordinator.mark_agent_session_running(run_id, running.session_id)
        pending = await coordinator.prepare_agent_session(
            run_id,
            agent_role=AgentRole.WATERBODY,
            target_comid="009999",
        )

        cancelled_run = await coordinator.cancel_run(run_id)
        by_id = {session.session_id: session for session in await coordinator.list_sessions(run_id)}

        assert cancelled_run.status is RunStatus.CANCELLED
        assert by_id[completed.session_id].status is AgentSessionStatus.COMPLETED
        assert by_id[running.session_id].status is AgentSessionStatus.CANCELLED
        assert by_id[pending.session_id].status is AgentSessionStatus.CANCELLED
        assert coordinator._runs[run_id].agent_sessions[completed.session_id].events[-1].type.value == "session_completed"
        for session_id in (running.session_id, pending.session_id):
            runtime = coordinator._runs[run_id].agent_sessions[session_id]
            assert runtime.stop_requested.is_set()
            assert runtime.events[-1].type.value == "session_cancelled"

    run_test(exercise)


def test_agent_session_cancel_has_priority_over_concurrent_completion() -> None:
    async def exercise(coordinator, clock, run_id) -> None:
        session = await coordinator.prepare_agent_session(
            run_id,
            agent_role=AgentRole.QUERY_PARSER,
        )
        await coordinator.mark_agent_session_running(run_id, session.session_id)
        runtime = coordinator._runs[run_id]
        async with runtime.condition:
            completion = asyncio.create_task(
                coordinator.complete_agent_session(run_id, session.session_id)
            )
            await asyncio.sleep(0)
            cancellation = asyncio.create_task(
                coordinator.cancel_agent_session(run_id, session.session_id)
            )
            await asyncio.sleep(0)
            assert runtime.agent_sessions[session.session_id].stop_requested.is_set()
        results = await asyncio.gather(
            completion,
            cancellation,
            return_exceptions=True,
        )

        assert all(result.status is AgentSessionStatus.CANCELLED for result in results)
        agent_runtime = runtime.agent_sessions[session.session_id]
        assert [event.type.value for event in agent_runtime.events] == [
            "session_created",
            "session_cancelled",
        ]

    run_test(exercise)


def test_run_scoped_transcript_identity_and_operation_are_stable() -> None:
    async def exercise(coordinator, clock, run_id) -> None:
        initial = await coordinator.list_transcript_items(run_id)
        assert [item.transcript_sequence for item in initial] == [1, 2]
        assert all(item.session_id is None for item in initial)

        parser = await coordinator.prepare_agent_session(
            run_id,
            agent_role=AgentRole.QUERY_PARSER,
        )
        bfs = await coordinator.reserve_transcript_item(run_id)
        waterbody = await coordinator.prepare_agent_session(
            run_id,
            agent_role=AgentRole.WATERBODY,
            target_comid="001234",
        )
        operation = await coordinator.create_operation(run_id, parser.session_id)
        first_message = await coordinator.reserve_transcript_item(
            run_id,
            session_id=parser.session_id,
            operation_id=operation.operation_id,
        )
        second_message = await coordinator.reserve_transcript_item(
            run_id,
            session_id=parser.session_id,
            operation_id=operation.operation_id,
        )
        clock_calls = clock.calls

        all_items = await coordinator.list_transcript_items(run_id)
        parser_items = await coordinator.list_session_transcript_items(
            run_id,
            parser.session_id,
        )
        waterbody_items = await coordinator.list_session_transcript_items(
            run_id,
            waterbody.session_id,
        )

        assert [item.transcript_sequence for item in all_items] == list(
            range(1, len(all_items) + 1)
        )
        assert bfs.session_id is None
        assert [item.transcript_sequence for item in parser_items] == [3, 6, 7]
        assert [item.transcript_sequence for item in waterbody_items] == [5]
        assert first_message.operation_id == second_message.operation_id == operation.operation_id
        assert first_message.created_at < second_message.created_at
        assert len({item.item_id for item in all_items}) == len(all_items)
        assert clock.calls == clock_calls
        assert await coordinator.list_transcript_items(run_id) == all_items
        assert clock.calls == clock_calls

    run_test(exercise)


def test_concurrent_transcript_allocation_is_unique_and_ordered() -> None:
    async def exercise(coordinator, clock, run_id) -> None:
        session = await coordinator.prepare_agent_session(
            run_id,
            agent_role=AgentRole.QUERY_PARSER,
        )
        allocated = await asyncio.gather(
            *(
                coordinator.reserve_transcript_item(
                    run_id,
                    session_id=session.session_id if index % 2 else None,
                )
                for index in range(40)
            )
        )

        assert len({item.item_id for item in allocated}) == 40
        assert len({item.transcript_sequence for item in allocated}) == 40
        assert sorted(item.transcript_sequence for item in allocated) == list(
            range(4, 44)
        )
        assert [
            item.transcript_sequence
            for item in await coordinator.list_transcript_items(run_id)
        ] == list(range(1, 44))

    run_test(exercise)


def test_session_ownership_is_checked_against_run_root() -> None:
    async def exercise(coordinator, clock, first_run_id) -> None:
        second = await coordinator.create_run("other", CONFIGURATION)
        await coordinator._workflow.started.setdefault(second.run_id, asyncio.Event()).wait()
        session = await coordinator.prepare_agent_session(
            first_run_id,
            agent_role=AgentRole.QUERY_PARSER,
        )

        with pytest.raises(AgentSessionNotFoundError):
            await coordinator.get_session(second.run_id, session.session_id)
        with pytest.raises(AgentSessionNotFoundError):
            await coordinator.reserve_transcript_item(
                second.run_id,
                session_id=session.session_id,
            )
        with pytest.raises(AgentSessionNotFoundError):
            await coordinator.create_operation(second.run_id, session.session_id)

    run_test(exercise)


def test_running_mutations_exist_without_public_lifecycle_items() -> None:
    async def exercise(coordinator, clock, run_id) -> None:
        session = await coordinator.prepare_agent_session(
            run_id,
            agent_role=AgentRole.QUERY_PARSER,
        )
        await coordinator.mark_agent_session_running(run_id, session.session_id)
        mutations = await coordinator.list_state_mutations(run_id)

        assert [type(item) for item in mutations] == [
            RunStateMutation,
            RunStateMutation,
            SessionStateMutation,
            SessionStateMutation,
        ]
        assert mutations[1].snapshot.status is RunStatus.RUNNING
        assert mutations[1].transcript_item is None
        assert mutations[3].summary.status is AgentSessionStatus.RUNNING
        assert mutations[3].transcript_item is None

    run_test(exercise)


def test_no_new_session_can_be_prepared_after_run_terminal() -> None:
    async def exercise(coordinator, clock, run_id) -> None:
        await coordinator.cancel_run(run_id)
        with pytest.raises(InvalidRunTransitionError):
            await coordinator.prepare_agent_session(
                run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
        assert await coordinator.list_sessions(run_id) == ()

    run_test(exercise)


def test_cancel_request_blocks_session_preparation_before_terminal_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise(coordinator, clock, run_id) -> None:
        runtime = coordinator._runs[run_id]
        original_acquire = runtime.condition.acquire
        cancel_waiting = asyncio.Event()
        allow_cancel = asyncio.Event()

        async def gated_acquire() -> bool:
            if asyncio.current_task().get_name() == "gated-cancel":
                cancel_waiting.set()
                await allow_cancel.wait()
            return await original_acquire()

        monkeypatch.setattr(runtime.condition, "acquire", gated_acquire)
        cancel_task = asyncio.create_task(
            coordinator.cancel_run(run_id),
            name="gated-cancel",
        )
        await cancel_waiting.wait()

        assert runtime.cancel_requested.is_set()
        assert runtime.stop_requested.is_set()
        with pytest.raises(InvalidRunTransitionError):
            await coordinator.prepare_agent_session(
                run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
        assert await coordinator.list_sessions(run_id) == ()

        allow_cancel.set()
        cancelled = await cancel_task
        assert cancelled.status is RunStatus.CANCELLED
        assert await coordinator.list_sessions(run_id) == ()

    run_test(exercise)
