import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from uuid import UUID

import httpx
import uvicorn
from fastapi import FastAPI
from pydantic import TypeAdapter

from app.api.schemas.analysis_runs import AnalysisRunResponse
from app.application.analysis_runs.models import RunLifecycleEvent, RunStatus
from app.application.analysis_runs.workflow import WorkflowContext
from app.infrastructure.config import AppSettings
from app.main import create_app

BASE = "/api/analysis/runs"
EVENT = TypeAdapter(RunLifecycleEvent)


class SmokeServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.ready = asyncio.Event()

    def capture_signals(self):
        return nullcontext()

    async def startup(self, sockets=None) -> None:
        await super().startup(sockets=sockets)
        self.ready.set()

    async def serve(self, sockets=None) -> None:
        try:
            await super().serve(sockets=sockets)
        except SystemExit as error:
            raise RuntimeError("Uvicorn could not start the smoke application.") from error
        finally:
            self.ready.set()


@asynccontextmanager
async def running_server(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = SmokeServer(uvicorn.Config(
            app, host="127.0.0.1", port=0, loop="asyncio", lifespan="on",
            log_config=None, access_log=False, workers=1, timeout_graceful_shutdown=2,
        ))
        task = asyncio.create_task(server.serve(sockets=[listener]), name="run-smoke-server")
        try:
            await asyncio.wait_for(server.ready.wait(), timeout=10)
            if task.done():
                await task
            if not server.started:
                raise RuntimeError("Uvicorn could not start the smoke application.")
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False
            ) as client:
                yield client
        finally:
            server.should_exit = True
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)


class GatedWorkflow:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.exited = asyncio.Event()
        self.context: WorkflowContext | None = None

    async def run(self, context: WorkflowContext) -> None:
        self.context = context
        self.started.set()
        try:
            await self.release.wait()
            context.cancellation.checkpoint()
        finally:
            self.exited.set()


def request_id(response: httpx.Response) -> UUID:
    value = response.headers["x-request-id"]
    identifier = UUID(value)
    assert str(identifier) == value
    return identifier


def stream_headers(response: httpx.Response) -> None:
    assert response.status_code == 200, response.status_code
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.headers["cache-control"] == "no-cache"
    request_id(response)


async def read_frame(lines: AsyncIterator[str]) -> list[str]:
    frame = []
    async for line in lines:
        if not line:
            return frame
        frame.append(line)
    raise AssertionError("SSE closed before a complete frame was received.")


async def read_event(lines: AsyncIterator[str]) -> RunLifecycleEvent:
    frame = await read_frame(lines)
    while frame == [": keepalive"]:
        frame = await read_frame(lines)
    assert len(frame) == 3 and frame[2].startswith("data: "), frame
    event = EVENT.validate_json(frame[2][6:])
    assert frame[:2] == [f"id: {event.sequence}", f"event: {event.type.value}"], frame
    return event


async def verify_replay(
    client: httpx.AsyncClient,
    path: str,
    terminal: RunLifecycleEvent,
) -> None:
    async with client.stream("GET", path, headers={"Last-Event-ID": "1"}) as response:
        stream_headers(response)
        lines = response.aiter_lines()
        assert await read_event(lines) == terminal
        assert await anext(lines, None) is None, "Terminal stream did not close"
    consumed = await client.get(path, headers={"Last-Event-ID": str(terminal.sequence)})
    assert consumed.status_code == 204 and consumed.content == b""
    request_id(consumed)


async def completed_smoke() -> None:
    app = create_app(AppSettings(analysis_timezone="America/Chicago", log_level="warning"))
    async with running_server(app) as client:
        created = await client.post(BASE, json={"query": "M3 run smoke completed", "time_resolution": "6h"})
        assert created.status_code == 201, created.text
        snapshot = AnalysisRunResponse.model_validate(created.json())
        assert snapshot.status is RunStatus.PENDING
        assert snapshot.created_at == snapshot.updated_at
        assert snapshot.configuration.model_dump(mode="json") == {
            "analysis_timezone": "America/Chicago", "time_resolution": "6h",
            "time_resolution_source": "frontend",
        }
        path = f"{BASE}/{snapshot.run_id}/events"
        async with client.stream("GET", path) as response:
            stream_headers(response)
            assert request_id(response) != request_id(created)
            lines = response.aiter_lines()
            first = await read_event(lines)
            terminal = await read_event(lines)
            assert (first.type.value, terminal.type.value) == ("run_created", "run_completed")
            assert first.sequence == 1 and terminal.sequence == 2
            assert first.run_id == terminal.run_id == snapshot.run_id
            assert first.payload.configuration == snapshot.configuration
            assert first.occurred_at == snapshot.created_at
            assert await anext(lines, None) is None, "Terminal stream did not close"
        await verify_replay(client, path, terminal)


async def cancelled_smoke() -> None:
    workflow = GatedWorkflow()
    app = create_app(
        AppSettings(analysis_timezone="America/Chicago", log_level="warning"),
        workflow=workflow, keepalive_interval=0.01,
    )
    async with running_server(app) as client:
        try:
            created = await client.post(BASE, json={"query": "M3 run smoke cancelled"})
            assert created.status_code == 201, created.text
            snapshot = AnalysisRunResponse.model_validate(created.json())
            assert snapshot.status is RunStatus.PENDING
            assert snapshot.configuration.time_resolution.value == "12h"
            assert snapshot.configuration.time_resolution_source.value == "default"
            await workflow.started.wait()
            path = f"{BASE}/{snapshot.run_id}/events"
            async with client.stream("GET", path) as response:
                stream_headers(response)
                lines = response.aiter_lines()
                first = await read_event(lines)
                assert first.type.value == "run_created" and first.sequence == 1
                assert first.run_id == snapshot.run_id
                assert await read_frame(lines) == [": keepalive"]
            assert workflow.context is not None
            assert not workflow.context.cancellation.is_requested()
            async with client.stream("GET", path, headers={"Last-Event-ID": "1"}) as response:
                stream_headers(response)
                lines = response.aiter_lines()
                assert await read_frame(lines) == [": keepalive"]
                cancelled = await client.post(f"{BASE}/{snapshot.run_id}/cancel")
                assert cancelled.status_code == 200, cancelled.text
                cancellation = AnalysisRunResponse.model_validate(cancelled.json())
                assert cancellation.status is RunStatus.CANCELLED
                assert cancellation.run_id == snapshot.run_id
                assert cancellation.configuration == snapshot.configuration
                assert len({request_id(created), request_id(response), request_id(cancelled)}) == 3
                assert not workflow.exited.is_set(), "Cancel waited for the workflow gate"
                assert workflow.context.cancellation.is_requested()
                terminal = await read_event(lines)
                assert terminal.type.value == "run_cancelled" and terminal.sequence == 2
                assert terminal.run_id == snapshot.run_id
                assert terminal.payload.cancelled_at == cancellation.updated_at
                assert await anext(lines, None) is None, "Terminal stream did not close"
            repeated = await client.post(f"{BASE}/{snapshot.run_id}/cancel")
            assert repeated.status_code == 200 and repeated.json() == cancelled.json()
            assert request_id(repeated) != request_id(cancelled)
            workflow.release.set()
            await workflow.exited.wait()
            await verify_replay(client, path, terminal)
        finally:
            workflow.release.set()


async def main() -> None:
    async with asyncio.timeout(30):
        await completed_smoke()
        await cancelled_smoke()
    print(json.dumps({"completed": "passed", "cancelled": "passed"}, separators=(",", ":")))


if __name__ == "__main__":
    asyncio.run(main())
