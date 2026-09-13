import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from app.infrastructure.config import AppSettings
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from app.main import create_app
from scripts.run_sse_smoke import GatedWorkflow, running_server


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
