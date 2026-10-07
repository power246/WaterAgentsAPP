import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from uuid import UUID

import httpx
import uvicorn
from fastapi import FastAPI

from app.api.schemas.analysis_runs import AnalysisRunResponse, RunStatusResponse
from app.application.analysis_runs.models import RunStatus
from app.application.analysis_runs.workflow import WorkflowContext
from app.infrastructure.config import AppSettings
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from app.infrastructure.runtime.event_loop import configure_asyncio_event_loop
from app.main import create_app
try:
    from scripts.m4_request import smoke_request
    from scripts.m6_fixture_runtime import build_fixture_m6_runtime
except ModuleNotFoundError:  # Direct `python scripts/run_sse_smoke.py` execution.
    from m4_request import smoke_request
    from m6_fixture_runtime import build_fixture_m6_runtime

BASE = "/api/analysis/runs"
SMOKE_SETTINGS = AppSettings(
    analysis_timezone="America/Chicago",
    log_level="warning",
    model_catalog=(
        {
            "provider": "fixture",
            "model": "parser-v1",
            "roles": ["query_parser"],
        },
        {
            "provider": "fixture",
            "model": "waterbody-v1",
            "roles": ["waterbody"],
            "context_window_tokens": 128_000,
            "max_output_tokens": 32_768,
        },
        {
            "provider": "fixture",
            "model": "embedding-v1",
            "roles": ["embedding"],
            "embedding_profile": {
                "provider": "fixture",
                "model": "embedding-v1",
                "output_dimensions": 8,
                "max_input_tokens": 512,
                "distance_metric": "cosine",
                "semantic_min_similarity": 0.78,
                "canonical_query_schema_version": 1,
                "canonical_template_version": 1,
                "projection_schema_version": 1,
            },
        },
    ),
)


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


class CompleteWorkflow:
    async def run(self, context: WorkflowContext) -> None:
        context.cancellation.checkpoint()


def request_id(response: httpx.Response) -> UUID:
    value = response.headers["x-request-id"]
    identifier = UUID(value)
    assert str(identifier) == value
    return identifier


def stream_headers(response: httpx.Response) -> None:
    assert response.status_code == 200, response.status_code
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    request_id(response)


async def read_frame(lines: AsyncIterator[str]) -> list[str]:
    frame = []
    async for line in lines:
        if not line:
            return frame
        frame.append(line)
    raise AssertionError("SSE closed before a complete frame was received.")


async def read_event(lines: AsyncIterator[str]) -> tuple[str, dict[str, object], int | None]:
    frame = await read_frame(lines)
    while frame == [": keepalive"]:
        frame = await read_frame(lines)
    event_id = None
    if frame[0].startswith("id: "):
        event_id = int(frame.pop(0)[4:])
    assert len(frame) == 2 and frame[0].startswith("event: ") and frame[1].startswith("data: ")
    return frame[0][7:], json.loads(frame[1][6:]), event_id


async def verify_terminal_reconnect(client: httpx.AsyncClient, path: str, run_id: UUID) -> None:
    async with client.stream("GET", path, headers={"Last-Event-ID": "999"}) as response:
        stream_headers(response)
        lines = response.aiter_lines()
        name, snapshot, event_id = await read_event(lines)
        assert name == "run_snapshot" and snapshot["run_id"] == str(run_id)
        assert event_id is None
        name, end, event_id = await read_event(lines)
        assert name == "stream_end" and end["stream"] == "run"
        assert event_id is None
        assert await anext(lines, None) is None


async def completed_smoke() -> None:
    app = create_app(
        SMOKE_SETTINGS,
        workflow=CompleteWorkflow(),
        transcript_store=InMemoryTranscriptStore(),
        m6_runtime_builder=build_fixture_m6_runtime,
    )
    async with running_server(app) as client:
        created = await client.post(
            BASE,
            json=smoke_request(
                "M3 run smoke completed",
                time_resolution="6h",
                settings=SMOKE_SETTINGS,
            ),
        )
        assert created.status_code == 201, created.text
        snapshot = AnalysisRunResponse.model_validate(created.json())
        assert snapshot.status is RunStatus.PENDING
        path = f"{BASE}/{snapshot.run_id}/events"
        async with client.stream("GET", path) as response:
            stream_headers(response)
            assert request_id(response) != request_id(created)
            lines = response.aiter_lines()
            statuses = []
            final_snapshot = None
            while True:
                name, data, event_id = await read_event(lines)
                if name == "stream_end":
                    end = data
                    break
                assert name == "run_snapshot" and data["run_id"] == str(snapshot.run_id)
                assert data["status"] in {"pending", "running", "completed"}
                assert data["sessions"] == [] and data["waterbodies"] == []
                assert "configuration" not in data and event_id is None
                statuses.append(data["status"])
                final_snapshot = data
            assert final_snapshot is not None
            assert final_snapshot["status"] == "completed"
            assert final_snapshot["persistence"]["state"] == "up_to_date"
            assert name == "stream_end" and end["stream"] == "run" and event_id is None
            assert await anext(lines, None) is None
        await verify_terminal_reconnect(client, path, snapshot.run_id)


async def cancelled_smoke() -> None:
    workflow = GatedWorkflow()
    app = create_app(
        SMOKE_SETTINGS,
        workflow=workflow,
        keepalive_interval=0.01,
        transcript_store=InMemoryTranscriptStore(),
        m6_runtime_builder=build_fixture_m6_runtime,
    )
    async with running_server(app) as client:
        try:
            created = await client.post(
                BASE,
                json=smoke_request(
                    "M3 run smoke cancelled",
                    settings=SMOKE_SETTINGS,
                ),
            )
            snapshot = AnalysisRunResponse.model_validate(created.json())
            await workflow.started.wait()
            path = f"{BASE}/{snapshot.run_id}/events"
            async with client.stream("GET", path) as response:
                stream_headers(response)
                lines = response.aiter_lines()
                name, data, event_id = await read_event(lines)
                assert name == "run_snapshot" and data["status"] == "running"
                assert event_id is None
                while True:
                    frame = await read_frame(lines)
                    if frame == [": keepalive"]:
                        break
                    assert frame[0] == "event: run_snapshot"
                    update = json.loads(frame[1].removeprefix("data: "))
                    assert update["status"] == "running"
            assert workflow.context is not None
            assert not workflow.context.cancellation.is_requested()
            async with client.stream("GET", path) as response:
                stream_headers(response)
                lines = response.aiter_lines()
                name, data, _ = await read_event(lines)
                assert name == "run_snapshot" and data["status"] == "running"
                cancelled = await client.post(f"{BASE}/{snapshot.run_id}/cancel")
                cancellation = RunStatusResponse.model_validate(cancelled.json())
                assert cancellation.status is RunStatus.CANCELLED
                assert workflow.context.cancellation.is_requested()
                workflow.release.set()
                final_snapshot = None
                while True:
                    name, data, event_id = await read_event(lines)
                    if name == "stream_end":
                        end = data
                        break
                    assert name == "run_snapshot" and event_id is None
                    final_snapshot = data
                assert final_snapshot is not None
                assert final_snapshot["status"] == "cancelled"
                assert final_snapshot["persistence"]["state"] == "up_to_date"
                assert name == "stream_end" and end["stream"] == "run"
                assert event_id is None and await anext(lines, None) is None
            repeated = await client.post(f"{BASE}/{snapshot.run_id}/cancel")
            assert repeated.status_code == 200
            assert RunStatusResponse.model_validate(repeated.json()).status is RunStatus.CANCELLED
            await workflow.exited.wait()
            await verify_terminal_reconnect(client, path, snapshot.run_id)
        finally:
            workflow.release.set()


async def main() -> None:
    async with asyncio.timeout(30):
        await completed_smoke()
        await cancelled_smoke()
    print(json.dumps({"completed": "passed", "cancelled": "passed"}, separators=(",", ":")))


if __name__ == "__main__":
    configure_asyncio_event_loop()
    asyncio.run(main())
