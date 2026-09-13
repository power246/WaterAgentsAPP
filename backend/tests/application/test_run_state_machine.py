import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.api.schemas.analysis_runs import (
    AnalysisRunResponse,
    CreateAnalysisRunRequest,
)
from app.application.analysis_runs.coordinator import (
    InvalidRunTransitionError,
    RunCoordinator,
    RunNotFoundError,
)
from app.application.analysis_runs.models import (
    PublicError,
    RunStatus,
)
from app.application.analysis_runs.workflow import WorkflowContext
from app.infrastructure.runtime.system_clock import SystemClock

ERROR = PublicError(
    error_id=UUID("0463cd90-6d0f-47e9-9656-b4aa86352c61"),
    code="workflow_failed",
    message="The analysis workflow failed.",
    failed_stage="analysis_workflow",
    retryable=False,
)
CONFIGURATION = CreateAnalysisRunRequest(
    query="Analyze water quality", time_resolution="6h"
).to_configuration_snapshot("America/Chicago")


class StepClock:
    def __init__(self) -> None:
        self.calls = 0
        self.instant = datetime(2026, 9, 8, 8, tzinfo=timezone(timedelta(hours=-5)))

    def now(self) -> datetime:
        self.calls += 1
        return self.instant + timedelta(seconds=self.calls)


class WaitingWorkflow:
    def __init__(self) -> None:
        self.started: dict[UUID, asyncio.Event] = {}

    async def run(self, context: WorkflowContext) -> None:
        self.started.setdefault(context.run.run_id, asyncio.Event()).set()
        await context.cancellation.wait()
        context.cancellation.checkpoint()


async def wait_running(coordinator: RunCoordinator, run_id: UUID):
    await coordinator._workflow.started.setdefault(run_id, asyncio.Event()).wait()
    return await coordinator.get_snapshot(run_id)


def run(exercise) -> None:
    async def managed() -> None:
        coordinators = []

        def coordinator_factory(*args, **kwargs):
            coordinator = RunCoordinator(*args, workflow=WaitingWorkflow(), **kwargs)
            coordinators.append(coordinator)
            return coordinator

        try:
            await exercise(coordinator_factory)
        finally:
            for coordinator in coordinators:
                await coordinator.shutdown()
                assert all(runtime.workflow_task.done() for runtime in coordinator._runs.values())

    asyncio.run(asyncio.wait_for(managed(), timeout=5))


class RecordingHandler(logging.Handler):
    def __init__(self, coordinator: RunCoordinator, *, fail: bool = False) -> None:
        super().__init__()
        self.coordinator = coordinator
        self.fail = fail
        self.records: list[logging.LogRecord] = []
        self.lock_states: list[tuple[bool, bool]] = []
        self.event_identities: list[bool] = []
        self.status_matches: list[bool] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.event_name not in {"lifecycle_event_committed", "run_status_changed"}:
            return
        runtime = self.coordinator._runs[record.correlation.run_id]
        self.records.append(record)
        self.lock_states.append(
            (self.coordinator._runs_lock.locked(), runtime.condition.locked())
        )
        self.event_identities.append(
            record.lifecycle_event is None
            or record.lifecycle_event is runtime.events[-1]
        )
        self.status_matches.append(runtime.snapshot.status == record.to_status)
        if self.fail:
            raise RuntimeError("Log handler unavailable")


async def invoke(coordinator: RunCoordinator, operation: str, run_id):
    if operation == "fail_run":
        return await coordinator.fail_run(run_id, ERROR)
    return await getattr(coordinator, operation)(run_id)


@pytest.mark.parametrize("operation", ["complete_run", "fail_run", "cancel_run"])
def test_lifecycle_commits_one_timestamp_and_event_per_public_transition(operation: str) -> None:
    async def exercise(coordinator_factory) -> None:
        clock = StepClock()
        coordinator = coordinator_factory(clock)
        created = await coordinator.create_run("  private query  ", CONFIGURATION)
        runtime = coordinator._runs[created.run_id]
        stream = await coordinator.subscribe(str(created.run_id).upper())
        created_event = await anext(stream)

        assert created.status is RunStatus.PENDING
        assert created.created_at == created.updated_at == created_event.occurred_at
        assert created_event.payload.created_at == created.created_at
        assert created.created_at.tzinfo is timezone.utc
        assert created_event.sequence == 1
        assert runtime.query == "private query"
        assert "private query" not in repr(runtime)
        assert not runtime.cancel_requested.is_set()
        assert not runtime.workflow_quiesced.is_set()
        assert runtime.workflow_task is not None
        assert not runtime.workflow_task.done()
        assert clock.calls == 1

        running = await wait_running(coordinator, created.run_id)
        assert running.status is RunStatus.RUNNING
        assert running.updated_at > created.updated_at
        assert runtime.events == [created_event]
        assert clock.calls == 2

        terminal = await invoke(coordinator, operation, created.run_id)
        terminal_event = await anext(stream)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

        assert created.status is RunStatus.PENDING
        assert terminal.created_at == created.created_at
        assert terminal.configuration is created.configuration
        assert terminal.updated_at == terminal_event.occurred_at
        timestamp_field = {
            "complete_run": "completed_at",
            "fail_run": "failed_at",
            "cancel_run": "cancelled_at",
        }[operation]
        assert getattr(terminal_event.payload, timestamp_field) == terminal.updated_at
        assert terminal.status == terminal_event.payload.status
        assert terminal_event.sequence == 2
        assert await coordinator.get_snapshot(created.run_id) is terminal
        assert clock.calls == 3
        assert runtime.cancel_requested.is_set() is (operation == "cancel_run")
        assert runtime.last_error is (ERROR if operation == "fail_run" else None)
        response = AnalysisRunResponse.model_validate(terminal, from_attributes=True)
        assert response.model_dump() == terminal.model_dump()
        assert "private query" not in response.model_dump_json()

    run(exercise)


def test_pending_cancel_is_atomic_and_idempotent() -> None:
    async def exercise(coordinator_factory) -> None:
        clock = StepClock()
        coordinator = coordinator_factory(clock)
        created = await coordinator.create_run("query", CONFIGURATION)
        cancelled = await coordinator.cancel_run(created.run_id)

        assert await coordinator.cancel_run(created.run_id) is cancelled
        assert clock.calls == 2
        events = [event async for event in await coordinator.subscribe(created.run_id)]
        assert [event.type.value for event in events] == ["run_created", "run_cancelled"]
        assert events[-1].occurred_at == cancelled.updated_at
        assert coordinator._runs[created.run_id].cancel_requested.is_set()

    run(exercise)


def test_pending_cannot_complete_before_running() -> None:
    async def exercise(coordinator_factory) -> None:
        clock = StepClock()
        coordinator = coordinator_factory(clock)
        created = await coordinator.create_run("query", CONFIGURATION)
        with pytest.raises(InvalidRunTransitionError) as error:
            await coordinator.complete_run(created.run_id)
        assert error.value.run is created
        assert await coordinator.get_snapshot(created.run_id) is created
        assert len(coordinator._runs[created.run_id].events) == 1
        assert clock.calls == 1

    run(exercise)


def test_pending_can_fail_during_preparation() -> None:
    async def exercise(coordinator_factory) -> None:
        coordinator = coordinator_factory(StepClock())
        created = await coordinator.create_run("query", CONFIGURATION)
        failed = await coordinator.fail_run(created.run_id, ERROR)

        assert failed.status is RunStatus.FAILED
        events = [event async for event in await coordinator.subscribe(created.run_id)]
        assert [event.type.value for event in events] == ["run_created", "run_failed"]
        assert events[-1].payload.error is ERROR

    run(exercise)


def test_running_cannot_be_started_twice() -> None:
    async def exercise(coordinator_factory) -> None:
        clock = StepClock()
        coordinator = coordinator_factory(clock)
        created = await coordinator.create_run("query", CONFIGURATION)
        running = await wait_running(coordinator, created.run_id)
        with pytest.raises(InvalidRunTransitionError) as error:
            await coordinator.mark_running(created.run_id)
        assert error.value.run is running
        assert clock.calls == 2

    run(exercise)


@pytest.mark.parametrize(
    "terminal_operation", ["complete_run", "fail_run", "cancel_run"]
)
@pytest.mark.parametrize(
    "operation", ["mark_running", "complete_run", "fail_run", "cancel_run"]
)
def test_terminal_states_cannot_be_overwritten(terminal_operation: str, operation: str) -> None:
    async def exercise(coordinator_factory) -> None:
        clock = StepClock()
        coordinator = coordinator_factory(clock)
        created = await coordinator.create_run("query", CONFIGURATION)
        await wait_running(coordinator, created.run_id)
        terminal = await invoke(coordinator, terminal_operation, created.run_id)
        runtime = coordinator._runs[created.run_id]
        original_events = tuple(runtime.events)
        original_error = runtime.last_error

        if terminal_operation == operation == "cancel_run":
            assert await coordinator.cancel_run(created.run_id) is terminal
        else:
            with pytest.raises(InvalidRunTransitionError) as error:
                await invoke(coordinator, operation, created.run_id)
            assert error.value.run is terminal
        assert await coordinator.get_snapshot(created.run_id) is terminal
        assert tuple(runtime.events) == original_events
        assert runtime.last_error is original_error
        assert runtime.cancel_requested.is_set() is (terminal_operation == "cancel_run")
        assert clock.calls == 3

    run(exercise)


@pytest.mark.parametrize("operation", ["complete_run", "fail_run"])
@pytest.mark.parametrize("cancel_first", [False, True])
def test_terminal_race_has_one_winner_and_one_terminal_event(operation, cancel_first) -> None:
    async def exercise(coordinator_factory) -> None:
        clock = StepClock()
        coordinator = coordinator_factory(clock)
        created = await coordinator.create_run("query", CONFIGURATION)
        await wait_running(coordinator, created.run_id)
        runtime = coordinator._runs[created.run_id]
        operations = (
            ["cancel_run", operation] if cancel_first else [operation, "cancel_run"]
        )
        ready = [asyncio.Event(), asyncio.Event()]

        async def compete(index: int):
            ready[index].set()
            return await invoke(coordinator, operations[index], created.run_id)

        async with runtime.condition:
            tasks = [asyncio.create_task(compete(index)) for index in range(2)]
            await asyncio.gather(*(gate.wait() for gate in ready))
        results = await asyncio.gather(*tasks, return_exceptions=True)

        winner = results[0]
        if cancel_first:
            assert isinstance(results[1], InvalidRunTransitionError)
            assert results[1].run is winner
        else:
            assert results[1] is winner
        assert winner.status is RunStatus.CANCELLED
        assert await coordinator.get_snapshot(created.run_id) is winner
        events = [event async for event in await coordinator.subscribe(created.run_id)]
        assert len(events) == 2
        assert [event.sequence for event in events] == [1, 2]
        assert events[-1].payload.status == winner.status
        assert events[-1].occurred_at == winner.updated_at
        assert winner.updated_at > winner.created_at

    run(exercise)


def test_runs_have_independent_locks_events_and_cancellation() -> None:
    async def exercise(coordinator_factory) -> None:
        coordinator = coordinator_factory(StepClock())
        first = await coordinator.create_run("first", CONFIGURATION)
        await wait_running(coordinator, first.run_id)
        first_runtime = coordinator._runs[first.run_id]
        ready = asyncio.Event()

        async def blocked_cancel():
            ready.set()
            return await coordinator.cancel_run(first.run_id)

        async with first_runtime.condition:
            task = asyncio.create_task(blocked_cancel())
            await ready.wait()
            assert not task.done()
            second = await coordinator.create_run("second", CONFIGURATION)
            await wait_running(coordinator, second.run_id)
            completed = await coordinator.complete_run(second.run_id)
            second_runtime = coordinator._runs[second.run_id]
            assert await coordinator.get_snapshot(second.run_id) is completed
            assert not second_runtime.cancel_requested.is_set()
            for name in ("condition", "events", "cancel_requested", "stop_requested", "workflow_quiesced", "workflow_task"):
                assert getattr(first_runtime, name) is not getattr(second_runtime, name)
        cancelled = await task

        assert cancelled.status is RunStatus.CANCELLED
        assert await coordinator.get_snapshot(second.run_id) is completed
        for snapshot in (cancelled, completed):
            events = [event async for event in await coordinator.subscribe(snapshot.run_id)]
            assert [event.sequence for event in events] == [1, 2]
            assert all(event.run_id == snapshot.run_id for event in events)
        assert set(coordinator._runs) == {first.run_id, second.run_id}

    run(exercise)


def test_commit_notifies_all_waiting_subscribers_without_exposing_runtime(monkeypatch) -> None:
    async def exercise(coordinator_factory) -> None:
        coordinator = coordinator_factory(StepClock())
        created = await coordinator.create_run("query", CONFIGURATION)
        runtime = coordinator._runs[created.run_id]
        first = await coordinator.subscribe(created.run_id)
        second = await coordinator.subscribe(created.run_id)
        assert await anext(first) is await anext(second)
        original_wait = runtime.condition.wait
        waiting = asyncio.Event()
        wait_count = 0

        async def observe_wait():
            nonlocal wait_count
            wait_count += 1
            if wait_count == 2:
                waiting.set()
            return await original_wait()

        monkeypatch.setattr(runtime.condition, "wait", observe_wait)
        tasks = [asyncio.create_task(anext(stream)) for stream in (first, second)]
        await waiting.wait()
        cancelled = await coordinator.cancel_run(created.run_id)
        events = await asyncio.gather(*tasks)

        assert events[0] is events[1] is runtime.events[-1]
        assert events[0].occurred_at == cancelled.updated_at
        assert events[0].payload.status is RunStatus.CANCELLED
        for stream in (first, second):
            with pytest.raises(StopAsyncIteration):
                await anext(stream)

    run(exercise)


def test_closed_subscriber_does_not_cancel_run_or_affect_other_subscribers() -> None:
    async def exercise(coordinator_factory) -> None:
        coordinator = coordinator_factory(StepClock())
        created = await coordinator.create_run("query", CONFIGURATION)
        first = await coordinator.subscribe(created.run_id)
        second = await coordinator.subscribe(created.run_id)
        await anext(first)
        await first.aclose()

        await wait_running(coordinator, created.run_id)
        completed = await coordinator.complete_run(created.run_id)
        assert [event.sequence async for event in second] == [1, 2]
        assert completed.status is RunStatus.COMPLETED
        assert not coordinator._runs[created.run_id].cancel_requested.is_set()

    run(exercise)


@pytest.mark.parametrize("fail_logger", [False, True])
def test_logging_runs_after_unlock_and_cannot_undo_committed_state(fail_logger: bool) -> None:
    async def exercise(coordinator_factory) -> None:
        logger = logging.Logger("run-state-test", level=logging.INFO)
        coordinator = coordinator_factory(StepClock(), logger=logger)
        handler = RecordingHandler(coordinator, fail=fail_logger)
        logger.addHandler(handler)
        created = await coordinator.create_run("private query", CONFIGURATION)
        await wait_running(coordinator, created.run_id)
        failed = await coordinator.fail_run(created.run_id, ERROR)
        events = [event async for event in await coordinator.subscribe(created.run_id)]

        assert len(events) == 2
        assert events[-1].payload.error is ERROR
        assert failed.status is RunStatus.FAILED
        assert len(handler.records) == 3
        assert handler.lock_states == [(False, False)] * 3
        assert all(handler.event_identities)
        assert all(handler.status_matches)
        assert [record.event_name for record in handler.records] == [
            "lifecycle_event_committed", "run_status_changed", "lifecycle_event_committed"
        ]
        assert handler.records[1].lifecycle_event is None
        assert handler.records[-1].correlation.error_id == ERROR.error_id
        assert handler.records[-1].correlation.event_sequence == 2
        assert handler.records[-1].timestamp == failed.updated_at
        assert handler.records[-1].from_status is RunStatus.RUNNING
        assert handler.records[-1].to_status is RunStatus.FAILED
        assert "private query" not in repr([record.__dict__ for record in handler.records])

    run(exercise)


@pytest.mark.parametrize(
    "operation",
    ["get_snapshot", "mark_running", "complete_run", "fail_run", "cancel_run", "subscribe"],
)
def test_lookup_distinguishes_invalid_uuid_and_unknown_run(operation: str) -> None:
    async def exercise(coordinator_factory) -> None:
        coordinator = coordinator_factory(StepClock())
        with pytest.raises(ValidationError):
            await invoke(coordinator, operation, "not-a-uuid")
        missing = uuid4()
        with pytest.raises(RunNotFoundError) as error:
            await invoke(coordinator, operation, str(missing))
        assert error.value.run_id == missing
        assert coordinator._runs == {}

    run(exercise)


def test_duplicate_or_invalid_generated_id_does_not_replace_an_existing_run() -> None:
    async def exercise(coordinator_factory) -> None:
        run_id = uuid4()
        coordinator = coordinator_factory(StepClock(), id_factory=lambda: run_id)
        original = await coordinator.create_run("original", CONFIGURATION)
        with pytest.raises(ValueError, match="already exists"):
            await coordinator.create_run("replacement", CONFIGURATION)
        assert await coordinator.get_snapshot(run_id) is original
        assert coordinator._runs[run_id].query == "original"
        invalid = coordinator_factory(StepClock(), id_factory=lambda: "invalid")
        with pytest.raises(ValidationError):
            await invalid.create_run("query", CONFIGURATION)
        assert invalid._runs == {}

    run(exercise)


def test_failed_validation_never_partially_commits_or_consumes_event_sequence() -> None:
    async def exercise(coordinator_factory) -> None:
        clock = StepClock()
        coordinator = coordinator_factory(clock)
        with pytest.raises(ValidationError):
            await coordinator.create_run("  ", CONFIGURATION)
        assert coordinator._runs == {}
        assert clock.calls == 0
        created = await coordinator.create_run("query", CONFIGURATION)
        running = await wait_running(coordinator, created.run_id)
        runtime = coordinator._runs[created.run_id]
        with pytest.raises(ValidationError):
            await coordinator.fail_run(created.run_id, None)
        assert clock.calls == 2
        clock.instant = datetime(2026, 9, 8)
        with pytest.raises(ValidationError):
            await coordinator.cancel_run(created.run_id)
        assert await coordinator.get_snapshot(created.run_id) is running
        assert len(runtime.events) == 1
        assert not runtime.cancel_requested.is_set()
        clock.instant = datetime(2026, 9, 9, tzinfo=timezone.utc)
        await coordinator.complete_run(created.run_id)
        assert [event.sequence for event in runtime.events] == [1, 2]

    run(exercise)


def test_system_clock_can_drive_a_run_without_test_configuration() -> None:
    async def exercise(coordinator_factory) -> None:
        coordinator = coordinator_factory(SystemClock())
        created = await coordinator.create_run("query", CONFIGURATION)
        await wait_running(coordinator, created.run_id)
        completed = await coordinator.complete_run(created.run_id)
        assert created.created_at.tzinfo is timezone.utc
        assert completed.updated_at.tzinfo is timezone.utc

    run(exercise)
