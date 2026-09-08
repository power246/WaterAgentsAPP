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
from app.application.analysis_sessions.models import SessionStatus
from app.main import create_app
from scripts.session_sse_smoke import BASE, SmokeServer, read_event


@pytest.fixture
def running_entrypoint(monkeypatch):
    loggers = [logging.getLogger(name) for name in (
        "wateragents", "uvicorn", "uvicorn.error", "uvicorn.access"
    )]
    original_logging = [
        (logger, logger.handlers[:], logger.level, logger.propagate, logger.disabled)
        for logger in loggers
    ]
    monkeypatch.setenv("WATERAGENTS_LOG_LEVEL", "info")
    monkeypatch.setenv("WATERAGENTS_HOST", "127.0.0.1")

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


def test_entrypoint_shutdown_finishes_with_an_open_sse_subscription(running_entrypoint, capsys):
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
        application = create_app(workflow=workflow)
        async with running_entrypoint(application) as (server, task, client):
            created = await client.post(BASE, json={"query": "shutdown regression"})
            assert created.status_code == 201
            session_id = created.json()["session_id"]
            await workflow.started.wait()
            async with client.stream("GET", f"{BASE}/{session_id}/events") as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                assert (await read_event(lines)).type.value == "session_created"
                server.should_exit = True
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
                assert workflow.stopped.is_set()
                assert (await application.state.session_coordinator.get_snapshot(session_id)).status is SessionStatus.RUNNING
                try:
                    async for line in lines:
                        assert not line.startswith("event:")
                except httpx.RemoteProtocolError:
                    pass
        assert not asyncio.all_tasks() - before

    asyncio.run(asyncio.wait_for(exercise(), timeout=15))
    output = capsys.readouterr()
    rows = [json.loads(line) for line in output.err.splitlines() if line.startswith("{")]
    assert any(row["event_name"] == "workflow_quiesced" for row in rows)
    assert any(row["event_name"] == "service_shutdown" for row in rows)
    assert not any(row.get("lifecycle_event", {}).get("type") == "session_cancelled" for row in rows)


def test_entrypoint_logs_exclude_url_credentials_and_private_query(running_entrypoint, capsys):
    secret = "REVIEW_SYNTHETIC_TOKEN"
    private_query = "REVIEW_PRIVATE_QUERY"

    async def exercise():
        async with running_entrypoint(create_app()) as (_, _, client):
            healthy = await client.get("/health", params={"api_key": secret, "query": private_query})
            assert healthy.status_code == 200
            unknown = await client.get(f"/{secret}")
            assert unknown.status_code == 404

    asyncio.run(asyncio.wait_for(exercise(), timeout=15))
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert private_query not in output.out + output.err
    rows = [json.loads(line) for line in output.err.splitlines() if line.startswith("{")]
    completed = [row for row in rows if row["event_name"] == "http_request_completed"]
    assert [row["status_code"] for row in completed] == [200, 404]
    assert completed[0]["route"] == "/health"
    assert all(row["request_id"] for row in completed)
