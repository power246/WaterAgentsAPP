import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from app.application.analysis_runs.models import AgentRole
from app.application.transcripts.models import GenerationStatus
from app.application.transcripts.registry import create_production_registry
from app.infrastructure.config import AppSettings
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from app.main import create_app
from scripts.run_sse_smoke import (
    BASE,
    GatedWorkflow,
    read_frame,
    running_server,
)
from scripts.transcript_fixtures import (
    FixtureMessageV1,
    fixture_message_registration,
)


@pytest.mark.parametrize("exit_mode", ["normal", "failure", "cancelled"])
@pytest.mark.parametrize(
    "probe_failure", [None, httpx.ConnectError, httpx.ConnectTimeout],
    ids=["real-socket", "connection-refused", "connection-timeout"],
)
def test_server_cleanup_closes_socket_and_workflow_on_client_exit(
    exit_mode, probe_failure, application_logs,
):
    async def exercise():
        before = asyncio.all_tasks()
        entered = asyncio.Event()
        hold_client = asyncio.Event()
        workflow = GatedWorkflow()
        app = create_app(
            AppSettings(log_level="warning"), workflow=workflow, shutdown_grace_period=0,
            transcript_store=InMemoryTranscriptStore(),
        )
        address = None

        async def run_client():
            nonlocal address
            async with running_server(app) as client:
                address = str(client.base_url)
                assert (await client.get("/health")).json() == {"status": "healthy"}
                response = await client.post("/api/analysis/runs", json={"query": "cleanup smoke"})
                assert response.status_code == 201
                await workflow.started.wait()
                entered.set()
                if exit_mode == "failure":
                    raise RuntimeError("Smoke client failed.")
                if exit_mode == "cancelled":
                    await hold_client.wait()

        task = asyncio.create_task(run_client())
        try:
            await entered.wait()
            if exit_mode == "cancelled":
                task.cancel()
            result, = await asyncio.gather(task, return_exceptions=True)
            if exit_mode == "normal":
                assert result is None
            elif exit_mode == "failure":
                assert isinstance(result, RuntimeError) and str(result) == "Smoke client failed."
            else:
                assert isinstance(result, asyncio.CancelledError)
            assert workflow.exited.is_set()
            assert not asyncio.all_tasks() - before

            def fail_probe(request):
                raise probe_failure("Closed-port connection probe failed.", request=request)

            transport = httpx.MockTransport(fail_probe) if probe_failure else None
            async with httpx.AsyncClient(timeout=1, trust_env=False, transport=transport) as client:
                with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
                    await client.get(address)
        finally:
            workflow.release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(asyncio.wait_for(exercise(), timeout=15))


def test_lifespan_startup_failure_is_reported_without_orphan_tasks(application_logs):
    @asynccontextmanager
    async def fail_startup(app):
        raise RuntimeError("Smoke startup failed.")
        yield

    async def exercise():
        before = asyncio.all_tasks()
        app = create_app(transcript_store=InMemoryTranscriptStore())
        app.router.lifespan_context = fail_startup
        with pytest.raises(RuntimeError, match="could not start"):
            async with running_server(app):
                pytest.fail("The client must not run after a startup failure.")
        assert not asyncio.all_tasks() - before

    asyncio.run(asyncio.wait_for(exercise(), timeout=15))


def test_long_transcript_stream_allows_another_run_and_reaches_stream_end(
    application_logs,
):
    async def exercise():
        workflow = GatedWorkflow()
        registry = create_production_registry().copy_with(
            fixture_message_registration()
        )
        app = create_app(
            AppSettings(log_level="warning"),
            workflow=workflow,
            transcript_store=InMemoryTranscriptStore(),
            transcript_registry=registry,
            shutdown_grace_period=0,
            keepalive_interval=0.01,
        )
        try:
            async with running_server(app) as client:
                first = await client.post(BASE, json={"query": "long transcript smoke"})
                assert first.status_code == 201
                await workflow.started.wait()
                coordinator = app.state.run_coordinator
                run_id = first.json()["run_id"]
                session = await coordinator.prepare_agent_session(
                    run_id,
                    agent_role=AgentRole.QUERY_PARSER,
                )
                await coordinator.mark_agent_session_running(run_id, session.session_id)
                message = await coordinator.start_transcript_message(
                    run_id,
                    session.session_id,
                    "fixture_message",
                    1,
                )
                expected = "https://example.com/" + "a" * (128 * 1024)
                path = f"{BASE}/{run_id}/sessions/{session.session_id}/events"
                allow_finalize = asyncio.Event()

                async def produce_transcript():
                    await coordinator.append_transcript_message_delta(
                        run_id,
                        message.item_id,
                        expected,
                    )
                    await allow_finalize.wait()
                    await coordinator.finalize_transcript_message(
                        run_id,
                        message.item_id,
                        FixtureMessageV1(
                            text=expected,
                            generation_status=GenerationStatus.COMPLETED,
                        ),
                    )
                    await coordinator.complete_agent_session(run_id, session.session_id)

                async def create_and_cancel_other_run():
                    created = await client.post(
                        BASE,
                        json={"query": "concurrent run smoke"},
                    )
                    assert created.status_code == 201
                    cancelled = await client.post(
                        f"{BASE}/{created.json()['run_id']}/cancel"
                    )
                    assert cancelled.status_code == 200
                    return created

                async with client.stream("GET", path) as response:
                    assert response.status_code == 200
                    lines = response.aiter_lines()
                    started = asyncio.get_running_loop().time()
                    producer = asyncio.create_task(produce_transcript())
                    other_run = asyncio.create_task(create_and_cancel_other_run())
                    created_while_streaming = await other_run
                    concurrent_elapsed = asyncio.get_running_loop().time() - started
                    assert concurrent_elapsed < 1.0

                    streamed = []
                    keepalive_received = False
                    while True:
                        frame = await read_frame(lines)
                        if frame == [": keepalive"]:
                            keepalive_received = True
                            allow_finalize.set()
                            continue
                        event_line = next(line for line in frame if line.startswith("event: "))
                        data_line = next(line for line in frame if line.startswith("data: "))
                        name = event_line[7:]
                        data = json.loads(data_line[6:])
                        if name == "message_delta":
                            streamed.append(data["payload"]["text"])
                        if name == "stream_end":
                            assert data["stream"] == "session"
                            break
                    await producer
                    assert created_while_streaming.status_code == 201
                    assert keepalive_received
                    assert "".join(streamed) == expected
        finally:
            workflow.release.set()

    asyncio.run(asyncio.wait_for(exercise(), timeout=15))
