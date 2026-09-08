import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.api.schemas.analysis_sessions import (
    AnalysisSessionResponse,
    CreateAnalysisSessionRequest,
)
from app.application.analysis_sessions.coordinator import (
    InvalidSessionTransitionError,
    SessionCoordinator,
    SessionNotFoundError,
)
from app.application.analysis_sessions.models import (
    PublicError,
    SessionStatus,
)
from app.infrastructure.runtime.system_clock import SystemClock

ERROR = PublicError(
    error_id=UUID("0463cd90-6d0f-47e9-9656-b4aa86352c61"),
    code="workflow_failed",
    message="The analysis workflow failed.",
    failed_stage="analysis_workflow",
    retryable=False,
)
CONFIGURATION = CreateAnalysisSessionRequest(
    query="Analyze water quality", time_resolution="6h"
).to_configuration_snapshot("America/Chicago")


class StepClock:
    def __init__(self) -> None:
        self.calls = 0
        self.instant = datetime(2026, 9, 8, 8, tzinfo=timezone(timedelta(hours=-5)))

    def now(self) -> datetime:
        self.calls += 1
        return self.instant + timedelta(seconds=self.calls)


class RecordingHandler(logging.Handler):
    def __init__(self, coordinator: SessionCoordinator, *, fail: bool = False) -> None:
        super().__init__()
        self.coordinator = coordinator
        self.fail = fail
        self.records: list[logging.LogRecord] = []
        self.lock_states: list[tuple[bool, bool]] = []
        self.event_identities: list[bool] = []
        self.status_matches: list[bool] = []

    def emit(self, record: logging.LogRecord) -> None:
        runtime = self.coordinator._sessions[record.correlation.session_id]
        self.records.append(record)
        self.lock_states.append(
            (self.coordinator._sessions_lock.locked(), runtime.condition.locked())
        )
        self.event_identities.append(
            record.lifecycle_event is None
            or record.lifecycle_event is runtime.events[-1]
        )
        self.status_matches.append(runtime.snapshot.status == record.to_status)
        if self.fail:
            raise RuntimeError("Log handler unavailable")


async def invoke(coordinator: SessionCoordinator, operation: str, session_id):
    if operation == "fail_session":
        return await coordinator.fail_session(session_id, ERROR)
    return await getattr(coordinator, operation)(session_id)


@pytest.mark.parametrize("operation", ["complete_session", "fail_session", "cancel_session"])
def test_lifecycle_commits_one_timestamp_and_event_per_public_transition(operation: str) -> None:
    async def exercise() -> None:
        clock = StepClock()
        coordinator = SessionCoordinator(clock)
        created = await coordinator.create_session("  private query  ", CONFIGURATION)
        runtime = coordinator._sessions[created.session_id]
        stream = await coordinator.subscribe(str(created.session_id).upper())
        created_event = await anext(stream)

        assert created.status is SessionStatus.PENDING
        assert created.created_at == created.updated_at == created_event.occurred_at
        assert created_event.payload.created_at == created.created_at
        assert created.created_at.tzinfo is timezone.utc
        assert created_event.sequence == 1
        assert runtime.query == "private query"
        assert "private query" not in repr(runtime)
        assert not runtime.cancel_requested.is_set()
        assert not runtime.workflow_quiesced.is_set()
        assert runtime.workflow_task is None
        assert clock.calls == 1

        running = await coordinator.mark_running(created.session_id)
        assert running.status is SessionStatus.RUNNING
        assert running.updated_at > created.updated_at
        assert runtime.events == [created_event]
        assert clock.calls == 2

        terminal = await invoke(coordinator, operation, created.session_id)
        terminal_event = await anext(stream)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

        assert created.status is SessionStatus.PENDING
        assert terminal.created_at == created.created_at
        assert terminal.configuration is created.configuration
        assert terminal.updated_at == terminal_event.occurred_at
        timestamp_field = {
            "complete_session": "completed_at",
            "fail_session": "failed_at",
            "cancel_session": "cancelled_at",
        }[operation]
        assert getattr(terminal_event.payload, timestamp_field) == terminal.updated_at
        assert terminal.status == terminal_event.payload.status
        assert terminal_event.sequence == 2
        assert await coordinator.get_snapshot(created.session_id) is terminal
        assert clock.calls == 3
        assert runtime.cancel_requested.is_set() is (operation == "cancel_session")
        assert runtime.last_error is (ERROR if operation == "fail_session" else None)
        response = AnalysisSessionResponse.model_validate(terminal, from_attributes=True)
        assert response.model_dump() == terminal.model_dump()
        assert "private query" not in response.model_dump_json()

    asyncio.run(exercise())


def test_pending_cancel_is_atomic_and_idempotent() -> None:
    async def exercise() -> None:
        clock = StepClock()
        coordinator = SessionCoordinator(clock)
        created = await coordinator.create_session("query", CONFIGURATION)
        cancelled = await coordinator.cancel_session(created.session_id)

        assert await coordinator.cancel_session(created.session_id) is cancelled
        assert clock.calls == 2
        events = [event async for event in await coordinator.subscribe(created.session_id)]
        assert [event.type.value for event in events] == ["session_created", "session_cancelled"]
        assert events[-1].occurred_at == cancelled.updated_at
        assert coordinator._sessions[created.session_id].cancel_requested.is_set()

    asyncio.run(exercise())


@pytest.mark.parametrize("operation", ["complete_session", "fail_session"])
def test_pending_cannot_complete_or_fail_before_running(operation: str) -> None:
    async def exercise() -> None:
        clock = StepClock()
        coordinator = SessionCoordinator(clock)
        created = await coordinator.create_session("query", CONFIGURATION)
        with pytest.raises(InvalidSessionTransitionError) as error:
            await invoke(coordinator, operation, created.session_id)
        assert error.value.session is created
        assert await coordinator.get_snapshot(created.session_id) is created
        assert len(coordinator._sessions[created.session_id].events) == 1
        assert clock.calls == 1

    asyncio.run(exercise())


def test_running_cannot_be_started_twice() -> None:
    async def exercise() -> None:
        clock = StepClock()
        coordinator = SessionCoordinator(clock)
        created = await coordinator.create_session("query", CONFIGURATION)
        running = await coordinator.mark_running(created.session_id)
        with pytest.raises(InvalidSessionTransitionError) as error:
            await coordinator.mark_running(created.session_id)
        assert error.value.session is running
        assert clock.calls == 2

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "terminal_operation", ["complete_session", "fail_session", "cancel_session"]
)
@pytest.mark.parametrize(
    "operation", ["mark_running", "complete_session", "fail_session", "cancel_session"]
)
def test_terminal_states_cannot_be_overwritten(terminal_operation: str, operation: str) -> None:
    async def exercise() -> None:
        clock = StepClock()
        coordinator = SessionCoordinator(clock)
        created = await coordinator.create_session("query", CONFIGURATION)
        await coordinator.mark_running(created.session_id)
        terminal = await invoke(coordinator, terminal_operation, created.session_id)
        runtime = coordinator._sessions[created.session_id]
        original_events = tuple(runtime.events)
        original_error = runtime.last_error

        if terminal_operation == operation == "cancel_session":
            assert await coordinator.cancel_session(created.session_id) is terminal
        else:
            with pytest.raises(InvalidSessionTransitionError) as error:
                await invoke(coordinator, operation, created.session_id)
            assert error.value.session is terminal
        assert await coordinator.get_snapshot(created.session_id) is terminal
        assert tuple(runtime.events) == original_events
        assert runtime.last_error is original_error
        assert runtime.cancel_requested.is_set() is (terminal_operation == "cancel_session")
        assert clock.calls == 3

    asyncio.run(exercise())


@pytest.mark.parametrize("operation", ["complete_session", "fail_session"])
@pytest.mark.parametrize("cancel_first", [False, True])
def test_terminal_race_has_one_winner_and_one_terminal_event(operation, cancel_first) -> None:
    async def exercise() -> None:
        clock = StepClock()
        coordinator = SessionCoordinator(clock)
        created = await coordinator.create_session("query", CONFIGURATION)
        await coordinator.mark_running(created.session_id)
        runtime = coordinator._sessions[created.session_id]
        operations = (
            ["cancel_session", operation] if cancel_first else [operation, "cancel_session"]
        )
        ready = [asyncio.Event(), asyncio.Event()]

        async def compete(index: int):
            ready[index].set()
            return await invoke(coordinator, operations[index], created.session_id)

        async with runtime.condition:
            tasks = [asyncio.create_task(compete(index)) for index in range(2)]
            await asyncio.gather(*(gate.wait() for gate in ready))
        winner, loser = await asyncio.gather(*tasks, return_exceptions=True)

        assert not isinstance(winner, Exception)
        assert isinstance(loser, InvalidSessionTransitionError)
        assert loser.session is winner
        assert winner.status.value == {
            "complete_session": "completed",
            "fail_session": "failed",
            "cancel_session": "cancelled",
        }[operations[0]]
        assert await coordinator.get_snapshot(created.session_id) is winner
        events = [event async for event in await coordinator.subscribe(created.session_id)]
        assert len(events) == 2
        assert [event.sequence for event in events] == [1, 2]
        assert events[-1].payload.status == winner.status
        assert events[-1].occurred_at == winner.updated_at
        assert clock.calls == 3

    asyncio.run(asyncio.wait_for(exercise(), timeout=5))


def test_sessions_have_independent_locks_events_and_cancellation() -> None:
    async def exercise() -> None:
        coordinator = SessionCoordinator(StepClock())
        first = await coordinator.create_session("first", CONFIGURATION)
        first_runtime = coordinator._sessions[first.session_id]
        ready = asyncio.Event()

        async def blocked_cancel():
            ready.set()
            return await coordinator.cancel_session(first.session_id)

        async with first_runtime.condition:
            task = asyncio.create_task(blocked_cancel())
            await ready.wait()
            assert not task.done()
            second = await coordinator.create_session("second", CONFIGURATION)
            await coordinator.mark_running(second.session_id)
            completed = await coordinator.complete_session(second.session_id)
            second_runtime = coordinator._sessions[second.session_id]
            assert await coordinator.get_snapshot(second.session_id) is completed
            assert not second_runtime.cancel_requested.is_set()
            for name in ("condition", "events", "cancel_requested", "workflow_quiesced"):
                assert getattr(first_runtime, name) is not getattr(second_runtime, name)
        cancelled = await task

        assert cancelled.status is SessionStatus.CANCELLED
        assert await coordinator.get_snapshot(second.session_id) is completed
        for snapshot in (cancelled, completed):
            events = [event async for event in await coordinator.subscribe(snapshot.session_id)]
            assert [event.sequence for event in events] == [1, 2]
            assert all(event.session_id == snapshot.session_id for event in events)
        assert set(coordinator._sessions) == {first.session_id, second.session_id}

    asyncio.run(asyncio.wait_for(exercise(), timeout=5))


def test_commit_notifies_all_waiting_subscribers_without_exposing_runtime(monkeypatch) -> None:
    async def exercise() -> None:
        coordinator = SessionCoordinator(StepClock())
        created = await coordinator.create_session("query", CONFIGURATION)
        runtime = coordinator._sessions[created.session_id]
        first = await coordinator.subscribe(created.session_id)
        second = await coordinator.subscribe(created.session_id)
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
        cancelled = await coordinator.cancel_session(created.session_id)
        events = await asyncio.gather(*tasks)

        assert events[0] is events[1] is runtime.events[-1]
        assert events[0].occurred_at == cancelled.updated_at
        assert events[0].payload.status is SessionStatus.CANCELLED
        for stream in (first, second):
            with pytest.raises(StopAsyncIteration):
                await anext(stream)

    asyncio.run(asyncio.wait_for(exercise(), timeout=5))


def test_closed_subscriber_does_not_cancel_session_or_affect_other_subscribers() -> None:
    async def exercise() -> None:
        coordinator = SessionCoordinator(StepClock())
        created = await coordinator.create_session("query", CONFIGURATION)
        first = await coordinator.subscribe(created.session_id)
        second = await coordinator.subscribe(created.session_id)
        await anext(first)
        await first.aclose()

        await coordinator.mark_running(created.session_id)
        completed = await coordinator.complete_session(created.session_id)
        assert [event.sequence async for event in second] == [1, 2]
        assert completed.status is SessionStatus.COMPLETED
        assert not coordinator._sessions[created.session_id].cancel_requested.is_set()

    asyncio.run(exercise())


@pytest.mark.parametrize("fail_logger", [False, True])
def test_logging_runs_after_unlock_and_cannot_undo_committed_state(fail_logger: bool) -> None:
    async def exercise() -> None:
        logger = logging.Logger("session-state-test", level=logging.INFO)
        coordinator = SessionCoordinator(StepClock(), logger=logger)
        handler = RecordingHandler(coordinator, fail=fail_logger)
        logger.addHandler(handler)
        created = await coordinator.create_session("private query", CONFIGURATION)
        await coordinator.mark_running(created.session_id)
        failed = await coordinator.fail_session(created.session_id, ERROR)
        events = [event async for event in await coordinator.subscribe(created.session_id)]

        assert len(events) == 2
        assert events[-1].payload.error is ERROR
        assert failed.status is SessionStatus.FAILED
        assert len(handler.records) == 3
        assert handler.lock_states == [(False, False)] * 3
        assert all(handler.event_identities)
        assert all(handler.status_matches)
        assert [record.event_name for record in handler.records] == [
            "lifecycle_event_committed", "session_status_changed", "lifecycle_event_committed"
        ]
        assert handler.records[1].lifecycle_event is None
        assert handler.records[-1].correlation.error_id == ERROR.error_id
        assert handler.records[-1].correlation.event_sequence == 2
        assert handler.records[-1].timestamp == failed.updated_at
        assert handler.records[-1].from_status is SessionStatus.RUNNING
        assert handler.records[-1].to_status is SessionStatus.FAILED
        assert "private query" not in repr([record.__dict__ for record in handler.records])

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "operation",
    ["get_snapshot", "mark_running", "complete_session", "fail_session", "cancel_session", "subscribe"],
)
def test_lookup_distinguishes_invalid_uuid_and_unknown_session(operation: str) -> None:
    async def exercise() -> None:
        coordinator = SessionCoordinator(StepClock())
        with pytest.raises(ValidationError):
            await invoke(coordinator, operation, "not-a-uuid")
        missing = uuid4()
        with pytest.raises(SessionNotFoundError) as error:
            await invoke(coordinator, operation, str(missing))
        assert error.value.session_id == missing
        assert coordinator._sessions == {}

    asyncio.run(exercise())


def test_duplicate_or_invalid_generated_id_does_not_replace_an_existing_session() -> None:
    async def exercise() -> None:
        session_id = uuid4()
        coordinator = SessionCoordinator(StepClock(), id_factory=lambda: session_id)
        original = await coordinator.create_session("original", CONFIGURATION)
        with pytest.raises(ValueError, match="already exists"):
            await coordinator.create_session("replacement", CONFIGURATION)
        assert await coordinator.get_snapshot(session_id) is original
        assert coordinator._sessions[session_id].query == "original"
        invalid = SessionCoordinator(StepClock(), id_factory=lambda: "invalid")
        with pytest.raises(ValidationError):
            await invalid.create_session("query", CONFIGURATION)
        assert invalid._sessions == {}

    asyncio.run(exercise())


def test_failed_validation_never_partially_commits_or_consumes_event_sequence() -> None:
    async def exercise() -> None:
        clock = StepClock()
        coordinator = SessionCoordinator(clock)
        with pytest.raises(ValidationError):
            await coordinator.create_session("  ", CONFIGURATION)
        assert coordinator._sessions == {}
        assert clock.calls == 0
        created = await coordinator.create_session("query", CONFIGURATION)
        running = await coordinator.mark_running(created.session_id)
        runtime = coordinator._sessions[created.session_id]
        with pytest.raises(ValidationError):
            await coordinator.fail_session(created.session_id, None)
        assert clock.calls == 2
        clock.instant = datetime(2026, 9, 8)
        with pytest.raises(ValidationError):
            await coordinator.cancel_session(created.session_id)
        assert await coordinator.get_snapshot(created.session_id) is running
        assert len(runtime.events) == 1
        assert not runtime.cancel_requested.is_set()
        clock.instant = datetime(2026, 9, 8, tzinfo=timezone.utc)
        await coordinator.complete_session(created.session_id)
        assert [event.sequence for event in runtime.events] == [1, 2]

    asyncio.run(exercise())


def test_system_clock_can_drive_a_session_without_test_configuration() -> None:
    async def exercise() -> None:
        coordinator = SessionCoordinator(SystemClock())
        created = await coordinator.create_session("query", CONFIGURATION)
        await coordinator.mark_running(created.session_id)
        completed = await coordinator.complete_session(created.session_id)
        assert created.created_at.tzinfo is timezone.utc
        assert completed.updated_at.tzinfo is timezone.utc

    asyncio.run(exercise())
