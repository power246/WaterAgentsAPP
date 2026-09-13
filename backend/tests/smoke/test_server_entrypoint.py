import asyncio
import json
import logging
import socket
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn

import app.__main__ as entrypoint
import app.main as application_module
from app.application.analysis_runs.models import RunStatus
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from app.main import create_app
from scripts.run_sse_smoke import BASE, GatedWorkflow, SmokeServer, read_event


@pytest.fixture
def running_entrypoint(monkeypatch, request):
    loggers = [logging.getLogger(name) for name in (
        "wateragents", "uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi"
    )]
    original_logging = [
        (logger, logger.handlers[:], logger.level, logger.propagate, logger.disabled)
        for logger in loggers
    ]
    monkeypatch.setenv("WATERAGENTS_LOG_LEVEL", getattr(request, "param", "info"))
    monkeypatch.setenv("WATERAGENTS_HOST", "127.0.0.1")
    monkeypatch.setenv("WATERAGENTS_DB_PASSWORD", "entrypoint-test-only")

    @asynccontextmanager
    async def run(application):
        servers = []

        def capture_run(target, **options):
            assert target == "app.main:app"
            servers.append(SmokeServer(uvicorn.Config(target, **options)))

        monkeypatch.setattr(application_module, "app", application)
        monkeypatch.setattr(entrypoint.uvicorn, "run", capture_run)
        entrypoint.main()
        server, = servers
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            address = f"http://127.0.0.1:{listener.getsockname()[1]}"
            task = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                await asyncio.wait_for(server.ready.wait(), timeout=5)
                assert server.started
                async with httpx.AsyncClient(base_url=address, timeout=5, trust_env=False) as client:
                    yield server, task, client
            finally:
                server.should_exit = True
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=5)
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    try:
        yield run
    finally:
        for logger, handlers, level, propagate, disabled in original_logging:
            logger.handlers = handlers
            logger.setLevel(level)
            logger.propagate = propagate
            logger.disabled = disabled


def test_entrypoint_shutdown_finishes_with_an_open_sse_subscription(
    running_entrypoint, reset_stream_disconnect, capsys,
):
    class CooperativeWorkflow:
        def __init__(self):
            self.started = asyncio.Event()
            self.stopped = asyncio.Event()

        async def run(self, context):
            self.started.set()
            await context.cancellation.wait()
            self.stopped.set()
            context.cancellation.checkpoint()

    async def exercise():
        before = asyncio.all_tasks()
        workflow = CooperativeWorkflow()
        application = create_app(
            workflow=workflow,
            transcript_store=InMemoryTranscriptStore(),
        )
        async with running_entrypoint(application) as (server, task, client):
            created = await client.post(BASE, json={"query": "shutdown regression"})
            assert created.status_code == 201
            run_id = created.json()["run_id"]
            await workflow.started.wait()
            async with client.stream("GET", f"{BASE}/{run_id}/events") as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                assert (await read_event(lines)).type.value == "run_created"
                server.should_exit = True
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
                assert workflow.stopped.is_set()
                assert (await application.state.run_coordinator.get_snapshot(run_id)).status is RunStatus.RUNNING
                try:
                    async for line in lines:
                        assert not line.startswith("event:")
                except (httpx.RemoteProtocolError, httpx.ReadError):
                    pass
        assert not asyncio.all_tasks() - before

    asyncio.run(asyncio.wait_for(exercise(), timeout=15))
    output = capsys.readouterr()
    rows = [json.loads(line) for line in output.err.splitlines() if line.startswith("{")]
    assert any(row["event_name"] == "workflow_quiesced" for row in rows)
    assert any(row["event_name"] == "service_shutdown" for row in rows)
    assert not any(row.get("lifecycle_event", {}).get("type") == "run_cancelled" for row in rows)


@pytest.mark.parametrize("running_entrypoint", ["info", "trace"], indirect=True)
def test_entrypoint_logs_exclude_url_credentials_and_private_query(running_entrypoint, capsys):
    secret = "REVIEW_SYNTHETIC_TOKEN"
    private_query = "REVIEW_PRIVATE_QUERY"
    authorization = "REVIEW_AUTHORIZATION"
    cookie = "REVIEW_COOKIE"
    application = create_app(transcript_store=InMemoryTranscriptStore())

    async def exercise():
        async with running_entrypoint(application) as (_, _, client):
            logging.getLogger("wateragents").log(5, "Application trace remains available.")
            healthy = await client.get(
                "/health", params={"api_key": secret, "query": private_query},
                headers={"Authorization": f"Bearer {authorization}", "Cookie": f"sid={cookie}"},
            )
            assert healthy.status_code == 200
            unknown = await client.get(f"/{secret}")
            assert unknown.status_code == 404

    asyncio.run(asyncio.wait_for(exercise(), timeout=15))
    output = capsys.readouterr()
    for private_value in (secret, private_query, authorization, cookie):
        assert private_value not in output.out + output.err
    rows = [json.loads(line) for line in output.err.splitlines() if line.startswith("{")]
    trace_rows = [row for row in rows if row["message"] == "Application trace remains available."]
    assert bool(trace_rows) is (application.state.settings.log_level == "trace")
    assert all(row["level"] == "TRACE" for row in trace_rows)
    completed = [row for row in rows if row["event_name"] == "http_request_completed"]
    assert [row["status_code"] for row in completed] == [200, 404]
    assert completed[0]["route"] == "/health"
    assert all(row["request_id"] for row in completed)


@pytest.mark.parametrize("running_entrypoint", ["info", "trace"], indirect=True)
def test_stream_failure_is_safe_in_full_server_output_and_can_replay(
    running_entrypoint, reset_stream_disconnect, monkeypatch, capsys,
):
    from app.api.sse import response as response_module

    secret = "STREAM_SYNTHETIC_KEY"
    cause_secret = "STREAM_SYNTHETIC_CAUSE"
    original_encode = response_module.encode_event
    failed = False

    def fail_once(event):
        nonlocal failed
        if event.sequence == 2 and not failed:
            failed = True
            try:
                raise ValueError(f"upstream failure; access_token={cause_secret}")
            except ValueError as cause:
                raise RuntimeError(f"encoder failure; api_key={secret}") from cause
        return original_encode(event)

    monkeypatch.setattr(response_module, "encode_event", fail_once)

    async def exercise():
        before = asyncio.all_tasks()
        workflow = GatedWorkflow()
        application = create_app(
            workflow=workflow,
            transcript_store=InMemoryTranscriptStore(),
        )
        async with running_entrypoint(application) as (_, _, client):
            coordinator = application.state.run_coordinator
            original_subscribe = coordinator.subscribe
            subscriptions = []

            async def observe_subscription(*args, **options):
                events = await original_subscribe(*args, **options)
                if events is not None:
                    subscriptions.append(events)
                return events

            monkeypatch.setattr(coordinator, "subscribe", observe_subscription)
            created = await client.post(BASE, json={"query": "stream failure regression"})
            assert created.status_code == 201
            run_id = created.json()["run_id"]
            await workflow.started.wait()
            path = f"{BASE}/{run_id}/events"
            try:
                async with client.stream("GET", path) as stream:
                    assert stream.status_code == 200
                    request_id = stream.headers["x-request-id"]
                    lines = stream.aiter_lines()
                    assert (await read_event(lines)).type.value == "run_created"
                    workflow.release.set()
                    with pytest.raises((httpx.RemoteProtocolError, httpx.ReadError)):
                        async for line in lines:
                            pytest.fail(f"Unexpected data after stream failure: {line!r}")
                assert subscriptions[0].ag_frame is None
                assert (await coordinator.get_snapshot(run_id)).status is RunStatus.COMPLETED
                assert workflow.exited.is_set()
                assert not workflow.context.cancellation.is_requested()
                async with client.stream("GET", path, headers={"Last-Event-ID": "1"}) as replay:
                    assert replay.status_code == 200
                    lines = replay.aiter_lines()
                    terminal = await read_event(lines)
                    assert terminal.type.value == "run_completed" and terminal.sequence == 2
                    assert str(terminal.run_id) == run_id
                    assert await anext(lines, None) is None
                assert (await client.get(path, headers={"Last-Event-ID": "2"})).status_code == 204
            finally:
                workflow.release.set()
        assert all(events.ag_frame is None for events in subscriptions)
        assert not asyncio.all_tasks() - before
        return run_id, request_id

    run_id, request_id = asyncio.run(asyncio.wait_for(exercise(), timeout=15))
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert cause_secret not in output.out + output.err
    rows = [json.loads(line) for line in output.err.splitlines() if line.startswith("{")]
    failure, = [row for row in rows if row["event_name"] == "http_stream_failed"]
    assert failure["run_id"] == run_id and failure["request_id"] == request_id
    assert failure["exception_type"] == "RuntimeError"
    assert failure["exception_message"] == "encoder failure; api_key=[REDACTED]"
    assert "ValueError" in failure["stack_trace"] and "direct cause" in failure["stack_trace"]
    assert "access_token=[REDACTED]" in failure["stack_trace"]
    assert f"HTTP response stream aborted; request_id={request_id}" in output.err
