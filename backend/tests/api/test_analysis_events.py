import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError
from starlette.requests import ClientDisconnect

from app.api.schemas.analysis_runs import CreateAnalysisRunRequest
from app.api.sse.encoder import KEEPALIVE_FRAME, encode_event
from app.api.sse.response import create_event_response
from app.application.analysis_runs.coordinator import (
    EventCursorAheadError,
    InvalidEventCursorError,
    RunCoordinator,
    RunNotFoundError,
)
from app.application.analysis_runs.models import RunLifecycleEvent, RunStatus
from app.application.analysis_runs.workflow import WorkflowContext, WorkflowFailed
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from app.main import create_app

RUN_ID = "10ef14bd-d0e7-4a70-9728-1fbb4ca7a83c"
TIMESTAMP = "2026-09-08T03:20:00Z"
CONFIGURATION = CreateAnalysisRunRequest(query="query").to_configuration_snapshot(
    "America/Chicago"
)
EVENT_ADAPTER = TypeAdapter(RunLifecycleEvent)
PAYLOAD_JSON = {
    "created": '{"status":"pending","created_at":"2026-09-08T03:20:00Z",'
    '"configuration":{"analysis_timezone":"America/Chicago","time_resolution":"12h",'
    '"time_resolution_source":"default"}}',
    "completed": '{"status":"completed","completed_at":"2026-09-08T03:20:00Z",'
    '"warning":null}',
    "cancelled": '{"status":"cancelled","cancelled_at":"2026-09-08T03:20:00Z"}',
    "failed": '{"status":"failed","failed_at":"2026-09-08T03:20:00Z","error":'
    '{"error_id":"0463cd90-6d0f-47e9-9656-b4aa86352c61","code":"workflow_failed",'
    '"message":"The analysis workflow failed.","failed_stage":"analysis_workflow",'
    '"retryable":false}}',
}


def event_json(kind):
    sequence = 1 if kind == "created" else 2
    return (
        '{"schema_version":2,"run_id":"' + RUN_ID + '","sequence":'
        + str(sequence) + ',"occurred_at":"' + TIMESTAMP
        + '","session_id":null,"type":"run_'
        + kind + '","payload":' + PAYLOAD_JSON[kind] + '}'
    )


class CountingClock:
    def __init__(self) -> None:
        self.calls = 0

    def now(self) -> datetime:
        self.calls += 1
        return datetime(2026, 9, 8, 3, 20, tzinfo=timezone.utc)


class GatedWorkflow:
    def __init__(self) -> None:
        self.calls: asyncio.Queue[tuple[WorkflowContext, asyncio.Event]] = asyncio.Queue()
        self.error: Exception | None = None

    async def run(self, context: WorkflowContext) -> None:
        release = asyncio.Event()
        self.calls.put_nowait((context, release))
        await release.wait()
        context.cancellation.checkpoint()
        if self.error is not None:
            raise self.error


@asynccontextmanager
async def run_runtime(**options):
    workflow = GatedWorkflow()
    clock = CountingClock()
    options.setdefault("transcript_store", InMemoryTranscriptStore())
    application = create_app(
        clock=clock, workflow=workflow, shutdown_grace_period=0, **options
    )
    async with application.router.lifespan_context(application):
        coordinator = application.state.run_coordinator
        created = await coordinator.create_run("private query", CONFIGURATION)
        context, release = await workflow.calls.get()
        yield coordinator, created, context, release, workflow, clock
    assert all(runtime.workflow_task.done() for runtime in coordinator._runs.values())


def run(exercise) -> None:
    asyncio.run(asyncio.wait_for(exercise(), timeout=5))


async def collect_response(response):
    messages = []

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    return messages


def observe_wait(monkeypatch, runtime):
    waiting = asyncio.Event()
    original_wait = runtime.condition.wait

    async def wait():
        waiting.set()
        return await original_wait()

    monkeypatch.setattr(runtime.condition, "wait", wait)
    return waiting


@pytest.mark.parametrize("kind", ["created", "completed", "failed", "cancelled"])
def test_exact_lifecycle_frame(kind) -> None:
    sequence = 1 if kind == "created" else 2
    serialized = event_json(kind)
    event = EVENT_ADAPTER.validate_json(serialized)
    before = event.model_dump()
    assert encode_event(event) == (
        f"id: {sequence}\nevent: run_{kind}\ndata: {serialized}\n\n"
    ).encode("utf-8")
    assert event.model_dump() == before


def test_json_escaping_keeps_special_characters_inside_one_data_line() -> None:
    data = json.loads(event_json("failed"))
    message = '河流💧 "quoted" \\ slash\r\nnext\tcolumn\x00end'
    data["payload"]["error"]["message"] = message
    event = EVENT_ADAPTER.validate_python(data)
    frame = encode_event(event)
    assert frame.count(b"\n") == 4
    assert b"\r" not in frame
    assert b"\x00" not in frame
    assert "河流💧".encode("utf-8") in frame
    assert b'\\r\\n' in frame
    assert b'\\t' in frame
    assert b'\\u0000' in frame
    data_line = frame.decode("utf-8").split("\n")[2]
    assert json.loads(data_line.removeprefix("data: "))["payload"]["error"]["message"] == message
    assert EVENT_ADAPTER.validate_json(data_line.removeprefix("data: ")) == event


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
@pytest.mark.parametrize("cursor", [None, "0", "1", "2"])
def test_terminal_replay_and_consumed_cursor_response(outcome, cursor) -> None:
    async def exercise() -> None:
        async with run_runtime() as (coordinator, created, _, release, workflow, _):
            runtime = coordinator._runs[created.run_id]
            if outcome == "cancelled":
                await coordinator.cancel_run(created.run_id)
            elif outcome == "failed":
                workflow.error = WorkflowFailed("private diagnostic")
            release.set()
            await runtime.workflow_quiesced.wait()
            response = await create_event_response(coordinator, created.run_id, last_event_id=cursor)
            messages = await collect_response(response)
            assert messages[0]["status"] == (204 if cursor == "2" else 200)
            frames = [message["body"] for message in messages[1:] if message.get("body")]
            if cursor == "2":
                assert frames == []
                assert response.body == b""
                assert "content-type" not in response.headers
            else:
                expected = runtime.events[int(cursor or 0):]
                assert frames == [encode_event(event) for event in expected]
                assert messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
                assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
                assert response.headers["cache-control"] == "no-cache"
                assert "content-length" not in response.headers
                assert response._events.ag_frame is None
                assert response._frames.ag_frame is None
            assert len(runtime.events) == 2
            assert runtime.snapshot.status.value == outcome
            assert "private" not in repr(frames)

    run(exercise)


@pytest.mark.parametrize("cursor", ["", "-1", "+1", " 1", "1 ", "1\n", "1.0", "1e0", "0x1", "１", "١", "1_0", "a", "1,2"])
def test_invalid_header_is_rejected_before_response_creation(cursor) -> None:
    async def exercise() -> None:
        async with run_runtime() as (coordinator, created, _, _, _, _):
            with pytest.raises(InvalidEventCursorError) as error:
                await create_event_response(coordinator, created.run_id, last_event_id=cursor)
            assert str(error.value) == "The event cursor must be a non-negative decimal integer."
            assert (await coordinator.get_snapshot(created.run_id)).status is RunStatus.RUNNING

    run(exercise)


@pytest.mark.parametrize("cursor", [-1, True, False, 1.0, b"1", [], {}])
def test_application_cursor_rejects_coercible_or_wrong_types(cursor) -> None:
    async def exercise() -> None:
        async with run_runtime() as (coordinator, created, _, _, _, _):
            with pytest.raises(InvalidEventCursorError):
                await coordinator.subscribe(created.run_id, after_sequence=cursor)

    run(exercise)


def test_leading_zero_and_large_cursors_have_consistent_range_semantics() -> None:
    async def exercise() -> None:
        async with run_runtime() as (coordinator, created, _, _, _, _):
            for cursor in (0, "0000", "0" * 5000):
                stream = await coordinator.subscribe(created.run_id, after_sequence=cursor)
                assert (await anext(stream)).sequence == 1
                await stream.aclose()
            for cursor in (2, "0002", "9" * 5000, 10 ** 5000):
                with pytest.raises(EventCursorAheadError) as error:
                    await coordinator.subscribe(created.run_id, after_sequence=cursor)
                assert error.value.run.run_id == created.run_id
                assert error.value.last_sequence == 1
                assert str(error.value) == "The event cursor is ahead of the latest run event."

    run(exercise)


def test_missing_run_and_invalid_uuid_fail_before_response_creation() -> None:
    async def exercise() -> None:
        async with run_runtime() as (coordinator, created, _, _, _, _):
            with pytest.raises(RunNotFoundError) as error:
                await create_event_response(coordinator, str(uuid4()), last_event_id="0")
            assert error.value.run_id != created.run_id
            with pytest.raises(ValidationError):
                await create_event_response(coordinator, "not-a-uuid", last_event_id="0")
            assert len(coordinator._runs) == 1

    run(exercise)


def test_active_cursor_at_latest_waits_then_sends_only_new_event(monkeypatch) -> None:
    async def exercise() -> None:
        async with run_runtime() as (coordinator, created, _, release, _, _):
            runtime = coordinator._runs[created.run_id]
            waiting = observe_wait(monkeypatch, runtime)
            response = await create_event_response(coordinator, created.run_id, last_event_id="0001")
            task = asyncio.create_task(collect_response(response))
            try:
                await waiting.wait()
                assert not task.done()
                release.set()
                messages = await task
                assert [message["body"] for message in messages[1:] if message.get("body")] == [
                    encode_event(runtime.events[-1])
                ]
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    run(exercise)


@pytest.mark.parametrize("publish_at", ["before_first_read", "between_reads", "while_waiting"])
def test_replay_to_wait_has_no_gap_or_duplicate(publish_at, monkeypatch) -> None:
    async def exercise() -> None:
        async with run_runtime() as (coordinator, created, _, release, _, _):
            runtime = coordinator._runs[created.run_id]
            stream = await coordinator.subscribe(created.run_id)
            if publish_at == "before_first_read":
                release.set()
                await runtime.workflow_quiesced.wait()
            first = await anext(stream)
            assert first is runtime.events[0]
            if publish_at == "while_waiting":
                waiting = observe_wait(monkeypatch, runtime)
                next_event = asyncio.create_task(anext(stream))
                await waiting.wait()
                release.set()
                second = await next_event
            else:
                release.set()
                await runtime.workflow_quiesced.wait()
                second = await anext(stream)
            assert second is runtime.events[1]
            assert [first.sequence, second.sequence] == [1, 2]
            with pytest.raises(StopAsyncIteration):
                await anext(stream)

    run(exercise)


def test_published_event_wins_when_timeout_fires_while_waiting_for_the_lock(monkeypatch) -> None:
    async def exercise() -> None:
        async with run_runtime() as (coordinator, created, _, _, _, _):
            runtime = coordinator._runs[created.run_id]
            waiting = observe_wait(monkeypatch, runtime)
            original_timeout = asyncio.timeout
            timeouts = []

            def timeout(interval):
                timer = original_timeout(interval)
                timeouts.append(timer)
                return timer

            monkeypatch.setattr(asyncio, "timeout", timeout)
            stream = await coordinator.subscribe(created.run_id, after_sequence=1)
            subscriber = asyncio.create_task(anext(stream))
            await waiting.wait()
            publishing = asyncio.Event()

            async def publish():
                publishing.set()
                return await coordinator.cancel_run(created.run_id)

            async with runtime.condition:
                publisher = asyncio.create_task(publish())
                await publishing.wait()
                loop = asyncio.get_running_loop()
                timeouts[0].reschedule(loop.time())
                timer_fired = asyncio.Event()
                loop.call_soon(timer_fired.set)
                await timer_fired.wait()
                assert subscriber.cancelling() == 1
                assert not subscriber.done()
            cancelled = await publisher
            event = await subscriber
            assert timeouts[0].expired()
            assert event is runtime.events[-1]
            assert event.payload.status is cancelled.status
            assert event.sequence == 2
            with pytest.raises(StopAsyncIteration):
                await anext(stream)

    run(exercise)


def test_keepalive_is_transport_only_and_does_not_change_replay_or_logs(caplog, application_logs) -> None:
    caplog.set_level(logging.INFO, logger="wateragents.analysis_runs")

    async def exercise() -> None:
        async with run_runtime(keepalive_interval=0.000001) as (
            coordinator, created, context, release, _, clock
        ):
            runtime = coordinator._runs[created.run_id]
            snapshot = runtime.snapshot
            events = tuple(runtime.events)
            records = list(caplog.records)
            json_lines = list(application_logs)
            calls = clock.calls
            response = await create_event_response(coordinator, created.run_id, last_event_id="1")
            received = []
            two_comments = asyncio.Event()
            hold_send = asyncio.Event()
            tasks_before = asyncio.all_tasks()

            async def send(message):
                if message.get("body"):
                    received.append(message["body"])
                    if len(received) == 2:
                        two_comments.set()
                        await hold_send.wait()

            async def receive():
                await asyncio.Event().wait()

            task = asyncio.create_task(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
            try:
                await two_comments.wait()
                assert received == [b": keepalive\n\n"] * 2
                assert KEEPALIVE_FRAME == b": keepalive\n\n"
                assert runtime.snapshot is snapshot
                assert tuple(runtime.events) == events
                assert clock.calls == calls
                assert caplog.records == records
                assert application_logs == json_lines
                assert not context.cancellation.is_requested()
                assert asyncio.all_tasks() - tasks_before == {task}
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            assert response._events.ag_frame is None
            assert response._frames.ag_frame is None
            release.set()
            await runtime.workflow_quiesced.wait()
            reconnect = await create_event_response(coordinator, created.run_id, last_event_id="1")
            replay = await collect_response(reconnect)
            assert [message["body"] for message in replay[1:] if message.get("body")] == [
                encode_event(runtime.events[-1])
            ]

    run(exercise)


def test_multiple_subscribers_share_events_with_independent_cursors(monkeypatch) -> None:
    async def exercise() -> None:
        async with run_runtime() as (coordinator, created, context, release, _, _):
            runtime = coordinator._runs[created.run_id]
            slow = await coordinator.subscribe(created.run_id)
            fast = await coordinator.subscribe(created.run_id, after_sequence=1)
            closed = await coordinator.subscribe(created.run_id)
            assert await anext(closed) is runtime.events[0]
            await closed.aclose()
            waiting = observe_wait(monkeypatch, runtime)
            task = asyncio.create_task(anext(fast))
            await waiting.wait()
            assert not context.cancellation.is_requested()
            release.set()
            terminal = await task
            assert terminal is runtime.events[-1]
            assert [event async for event in slow] == runtime.events
            with pytest.raises(StopAsyncIteration):
                await anext(fast)
            assert runtime.snapshot.status is RunStatus.COMPLETED

    run(exercise)


@pytest.mark.parametrize("disconnect", ["receive", "cancel_task", "send_start", "send_body"])
def test_disconnect_or_write_failure_closes_only_its_subscription(disconnect, monkeypatch) -> None:
    async def exercise() -> None:
        async with run_runtime() as (coordinator, created, context, release, _, _):
            runtime = coordinator._runs[created.run_id]
            waiting = observe_wait(monkeypatch, runtime)
            other = await coordinator.subscribe(created.run_id, after_sequence=1)
            response = await create_event_response(coordinator, created.run_id)
            disconnected = asyncio.Event()

            async def send(message):
                if disconnect == "send_start" and message["type"] == "http.response.start":
                    raise OSError("client disconnected")
                if disconnect == "send_body" and message.get("body"):
                    raise OSError("client disconnected")

            async def receive():
                await disconnected.wait()
                return {"type": "http.disconnect"}

            scope = {"type": "http", "asgi": {"spec_version": "2.3" if disconnect == "receive" else "2.4"}}
            task = asyncio.create_task(response(scope, receive, send))
            try:
                if disconnect in {"receive", "cancel_task"}:
                    await waiting.wait()
                    if disconnect == "receive":
                        disconnected.set()
                        await task
                    else:
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await task
                else:
                    with pytest.raises(ClientDisconnect):
                        await task
                assert response._events.ag_frame is None
                assert response._frames.ag_frame is None
                assert runtime.snapshot.status is RunStatus.RUNNING
                assert not context.cancellation.is_requested()
                assert runtime.workflow_task.cancelling() == 0
                assert not runtime.workflow_task.done()
                assert len(runtime.events) == 1
                release.set()
                terminal = await anext(other)
                assert terminal.type.value == "run_completed"
                assert terminal is runtime.events[-1]
                await other.aclose()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    run(exercise)


@pytest.mark.parametrize("interval", [0, -1, float("inf"), float("nan"), True])
def test_keepalive_interval_must_be_finite_and_positive(interval) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        RunCoordinator(
            CountingClock(),
            transcript_store=InMemoryTranscriptStore(),
            keepalive_interval=interval,
        )


def test_default_interval_drives_the_real_subscription_wait(monkeypatch) -> None:
    async def exercise() -> None:
        async with run_runtime() as (coordinator, created, _, release, _, _):
            waiting = observe_wait(monkeypatch, coordinator._runs[created.run_id])
            original_timeout = asyncio.timeout
            intervals = []

            def timeout(interval):
                intervals.append(interval)
                return original_timeout(interval)

            monkeypatch.setattr(asyncio, "timeout", timeout)
            stream = await coordinator.subscribe(created.run_id, after_sequence=1)
            task = asyncio.create_task(anext(stream))
            await waiting.wait()
            release.set()
            assert (await task).sequence == 2
            await stream.aclose()
            assert intervals[0] == 15.0

    run(exercise)
