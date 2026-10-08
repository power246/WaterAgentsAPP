import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI

from app.application.analysis_runs.models import AgentRole
from app.application.analysis_runs.workflow import WorkflowContext
from app.application.transcripts.models import GenerationStatus
from app.application.transcripts.registry import create_production_registry
from app.infrastructure.config import AppSettings
from app.infrastructure.runtime.event_loop import (
    configure_asyncio_event_loop,
    uvicorn_loop,
)
from app.main import create_app
from health_smoke import available_loopback_port, stop_process
try:
    from scripts.m4_request import smoke_request
    from scripts.m6_fixture_runtime import build_fixture_m6_runtime
    from scripts.m8_fixture_runtime import build_fixture_m8_runtime
except ModuleNotFoundError:  # Direct `python scripts/run_history_smoke.py` execution.
    from m4_request import smoke_request
    from m6_fixture_runtime import build_fixture_m6_runtime
    from m8_fixture_runtime import build_fixture_m8_runtime
from run_sse_smoke import SMOKE_SETTINGS, read_event, stream_headers
from test_database import temporary_migrated_schema
from transcript_fixtures import FixtureMessageV1, fixture_message_registration

BASE = "/api/analysis/runs"
SCRIPT_ROOT = Path(__file__).resolve().parent
COMPLETED_QUERY = "M3 process history smoke with UTF-8"
CANCELLED_QUERY = "M3 process cancel smoke"
INTERRUPTED_QUERY = "M3 process restart repair smoke"
LARGE_TEXT = "流域结论：" + ("water-quality-observation " * 180)


class HistorySmokeWorkflow:
    def __init__(self) -> None:
        self._application: FastAPI | None = None

    def bind(self, application: FastAPI) -> None:
        self._application = application

    async def run(self, context: WorkflowContext) -> None:
        if self._application is None:
            raise RuntimeError("smoke workflow is not bound to the application")
        coordinator = self._application.state.run_coordinator
        run_id = context.run.run_id

        if context.query == COMPLETED_QUERY:
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
            for offset in range(0, len(LARGE_TEXT), 97):
                await coordinator.append_transcript_message_delta(
                    run_id,
                    message.item_id,
                    LARGE_TEXT[offset : offset + 97],
                )
            await coordinator.finalize_transcript_message(
                run_id,
                message.item_id,
                FixtureMessageV1(
                    text=LARGE_TEXT,
                    generation_status=GenerationStatus.COMPLETED,
                ),
            )
            await coordinator.complete_agent_session(run_id, session.session_id)
            return

        if context.query == CANCELLED_QUERY:
            await context.cancellation.wait()
            context.cancellation.checkpoint()

        if context.query == INTERRUPTED_QUERY:
            session = await coordinator.prepare_agent_session(
                run_id,
                agent_role=AgentRole.WATERBODY,
                target_comid="987654321",
            )
            await coordinator.mark_agent_session_running(run_id, session.session_id)
            await asyncio.Event().wait()

        raise RuntimeError(f"unexpected smoke query: {context.query!r}")


def create_smoke_app() -> FastAPI:
    workflow = HistorySmokeWorkflow()
    registry = create_production_registry().copy_with(
        fixture_message_registration()
    )
    application = create_app(
        AppSettings(log_level="warning", live_state_grace_seconds=60),
        workflow=workflow,
        transcript_registry=registry,
        shutdown_grace_period=0,
        keepalive_interval=0.05,
        m6_runtime_builder=build_fixture_m6_runtime,
        m8_runtime_builder=build_fixture_m8_runtime,
    )
    workflow.bind(application)
    return application


def start_application(settings: AppSettings) -> tuple[subprocess.Popen[str], int]:
    port = available_loopback_port()
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "WATERAGENTS_DB_NAME": settings.db_name,
            "WATERAGENTS_LOG_LEVEL": "warning",
            "WATERAGENTS_MODEL_CATALOG": json.dumps(
                [item.model_dump(mode="json") for item in settings.model_catalog],
                separators=(",", ":"),
            ),
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "run_history_smoke:create_smoke_app",
            "--factory",
            "--loop",
            uvicorn_loop(),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            "1",
            "--no-access-log",
            "--timeout-graceful-shutdown",
            "2",
            "--log-level",
            "warning",
        ],
        cwd=SCRIPT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
    )
    return process, port


async def wait_until_ready(
    process: subprocess.Popen[str],
    client: httpx.AsyncClient,
) -> None:
    deadline = asyncio.get_running_loop().time() + 20
    while asyncio.get_running_loop().time() < deadline:
        if process.poll() is not None:
            output = process.communicate()[0]
            raise RuntimeError(f"Uvicorn exited before readiness:\n{output}")
        try:
            response = await client.get("/health")
            response.raise_for_status()
            if response.json() == {"status": "healthy"}:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.1)
    raise TimeoutError("Uvicorn did not become healthy within 20 seconds")


@asynccontextmanager
async def running_application(
    settings: AppSettings,
) -> AsyncIterator[tuple[subprocess.Popen[str], httpx.AsyncClient]]:
    process, port = start_application(settings)
    client = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}",
        timeout=10,
        trust_env=False,
    )
    try:
        await wait_until_ready(process, client)
        yield process, client
    finally:
        await client.aclose()
        stop_process(process)


def assert_request_id(response: httpx.Response) -> None:
    request_id = response.headers["x-request-id"]
    assert str(UUID(request_id)) == request_id


async def collect_stream(
    client: httpx.AsyncClient,
    path: str,
    connected: asyncio.Event | None = None,
) -> list[tuple[str, dict[str, object], int | None]]:
    events = []
    async with client.stream("GET", path) as response:
        stream_headers(response)
        if connected is not None:
            connected.set()
        lines = response.aiter_lines()
        while True:
            event = await read_event(lines)
            events.append(event)
            if event[0] == "stream_end":
                assert await anext(lines, None) is None
                return events


async def wait_for_detail(
    client: httpx.AsyncClient,
    run_id: UUID,
    predicate: Callable[[dict[str, object]], bool],
) -> dict[str, object]:
    deadline = asyncio.get_running_loop().time() + 15
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"{BASE}/{run_id}")
        assert response.status_code == 200, response.text
        detail = response.json()
        if predicate(detail):
            return detail
        await asyncio.sleep(0.05)
    raise TimeoutError(f"run {run_id} did not reach the expected state")


async def create_saved_runs(client: httpx.AsyncClient) -> dict[str, object]:
    key = uuid4()
    created = await client.post(
        BASE,
        json=smoke_request(
            COMPLETED_QUERY,
            time_resolution="6h",
            settings=SMOKE_SETTINGS,
        ),
        headers={"Idempotency-Key": str(key)},
    )
    assert created.status_code == 201, created.text
    assert_request_id(created)
    receipt = created.json()
    completed_id = UUID(receipt["run_id"])
    run_events = await collect_stream(client, f"{BASE}/{completed_id}/events")
    assert run_events[-1][0] == "stream_end"

    completed = await wait_for_detail(
        client,
        completed_id,
        lambda detail: detail["run"]["status"] == "completed",
    )
    session_id = UUID(completed["sessions"][0]["session_id"])
    session_events = await collect_stream(
        client,
        f"{BASE}/{completed_id}/sessions/{session_id}/events",
    )
    names = [name for name, _, _ in session_events]
    for expected in (
        "transcript_item",
        "message_started",
        "message_delta",
        "message_finalized",
        "stream_end",
    ):
        assert expected in names
    finalized = next(
        payload for name, payload, _ in session_events if name == "message_finalized"
    )
    message_id = UUID(finalized["payload"]["item"]["item_id"])

    detail_response = await client.get(
        f"{BASE}/{completed_id}", params={"view": "history"}
    )
    transcript_response = await client.get(
        f"{BASE}/{completed_id}/sessions/{session_id}/transcript",
        params={"view": "history"},
    )
    content_response = await client.get(
        f"{BASE}/{completed_id}/transcript/{message_id}/content",
        params={"view": "history"},
    )
    for response in (detail_response, transcript_response, content_response):
        assert response.status_code == 200, response.text
        assert response.json()["source"] == "postgresql"
    assert LARGE_TEXT in content_response.json()["text"]

    cancelled = await client.post(
        BASE,
        json=smoke_request(CANCELLED_QUERY, settings=SMOKE_SETTINGS),
    )
    assert cancelled.status_code == 201, cancelled.text
    cancelled_id = UUID(cancelled.json()["run_id"])
    await wait_for_detail(
        client,
        cancelled_id,
        lambda detail: detail["run"]["status"] == "running",
    )
    connected = asyncio.Event()
    cancel_events_task = asyncio.create_task(
        collect_stream(client, f"{BASE}/{cancelled_id}/events", connected)
    )
    await connected.wait()
    cancelled_response = await client.post(f"{BASE}/{cancelled_id}/cancel")
    assert cancelled_response.status_code == 200, cancelled_response.text
    assert cancelled_response.json()["status"] == "cancelled"
    cancel_events = await cancel_events_task
    assert cancel_events[-1][0] == "stream_end"

    interrupted = await client.post(
        BASE,
        json=smoke_request(INTERRUPTED_QUERY, settings=SMOKE_SETTINGS),
    )
    assert interrupted.status_code == 201, interrupted.text
    interrupted_id = UUID(interrupted.json()["run_id"])
    interrupted_detail = await wait_for_detail(
        client,
        interrupted_id,
        lambda detail: (
            detail["run"]["status"] == "running"
            and bool(detail["sessions"])
            and detail["sessions"][0]["status"] == "running"
            and detail["persistence"]["state"] == "up_to_date"
        ),
    )
    interrupted_session_id = UUID(
        interrupted_detail["sessions"][0]["session_id"]
    )

    history = await client.get(BASE, params={"limit": 2})
    assert history.status_code == 200, history.text
    restart_cursor = history.json()["next_cursor"]
    assert restart_cursor
    return {
        "key": str(key),
        "receipt": receipt,
        "completed_id": str(completed_id),
        "session_id": str(session_id),
        "message_id": str(message_id),
        "cancelled_id": str(cancelled_id),
        "interrupted_id": str(interrupted_id),
        "interrupted_session_id": str(interrupted_session_id),
        "restart_cursor": restart_cursor,
        "detail": detail_response.json(),
        "transcript": transcript_response.json(),
        "content": content_response.json(),
    }


async def verify_restart(
    client: httpx.AsyncClient,
    state: dict[str, object],
) -> None:
    first_page = await client.get(BASE, params={"limit": 2})
    second_page = await client.get(
        BASE,
        params={"limit": 2, "cursor": state["restart_cursor"]},
    )
    for response in (first_page, second_page):
        assert response.status_code == 200, response.text
    listed = {
        item["run_id"]
        for page in (first_page, second_page)
        for item in page.json()["items"]
    }
    expected = {
        state["completed_id"],
        state["cancelled_id"],
        state["interrupted_id"],
    }
    assert listed == expected

    completed_id = state["completed_id"]
    session_id = state["session_id"]
    message_id = state["message_id"]
    responses = (
        await client.get(f"{BASE}/{completed_id}", params={"view": "history"}),
        await client.get(
            f"{BASE}/{completed_id}/sessions/{session_id}/transcript",
            params={"view": "history"},
        ),
        await client.get(
            f"{BASE}/{completed_id}/transcript/{message_id}/content",
            params={"view": "history"},
        ),
    )
    for response, key in zip(responses, ("detail", "transcript", "content")):
        assert response.status_code == 200, response.text
        assert response.json() == state[key]

    interrupted_id = state["interrupted_id"]
    repaired = await client.get(
        f"{BASE}/{interrupted_id}", params={"view": "history"}
    )
    repaired_session = await client.get(
        f"{BASE}/{interrupted_id}/sessions/{state['interrupted_session_id']}/transcript",
        params={"view": "history"},
    )
    assert repaired.status_code == 200, repaired.text
    assert repaired_session.status_code == 200, repaired_session.text
    assert repaired.json()["run"]["status"] == "failed"
    repair_evidence = json.dumps(
        (repaired.json(), repaired_session.json()), ensure_ascii=False
    )
    assert repair_evidence.count("service_interrupted") == 2

    for run_id in expected:
        response = await client.get(f"{BASE}/{run_id}/events")
        assert response.status_code == 410, response.text
        assert response.json()["history_url"].endswith(
            f"/api/analysis/runs/{run_id}?view=history"
        )

    repeated = await client.post(
        BASE,
        json=smoke_request(
            COMPLETED_QUERY,
            time_resolution="6h",
            settings=SMOKE_SETTINGS,
        ),
        headers={"Idempotency-Key": state["key"]},
    )
    assert repeated.status_code == 201, repeated.text
    assert repeated.json() == state["receipt"]
    final_history = await client.get(BASE, params={"limit": 10})
    assert len(final_history.json()["items"]) == 3


async def run_smoke(settings: AppSettings) -> None:
    settings = settings.model_copy(
        update={"model_catalog": SMOKE_SETTINGS.model_catalog}
    )
    async with asyncio.timeout(90):
        async with running_application(settings) as (first_process, first_client):
            first_pid = first_process.pid
            state = await create_saved_runs(first_client)
        assert first_process.poll() is not None

        async with running_application(settings) as (second_process, second_client):
            assert second_process.pid != first_pid
            await verify_restart(second_client, state)
        assert second_process.poll() is not None
    print(
        json.dumps(
            {
                "history": "passed",
                "independent_process_restart": "passed",
                "restart_repair": "passed",
                "database": settings.db_name,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def main() -> None:
    with temporary_migrated_schema() as settings:
        asyncio.run(run_smoke(settings))


if __name__ == "__main__":
    configure_asyncio_event_loop()
    main()
