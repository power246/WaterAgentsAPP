import asyncio
import json
from uuid import UUID, uuid4

import httpx

from app.application.analysis_runs.models import AgentRole
from app.application.analysis_runs.persistence import PersistenceState
from app.application.analysis_runs.workflow import WorkflowContext
from app.application.transcripts.models import FixtureMessageV1, GenerationStatus
from app.application.transcripts.registry import (
    create_production_registry,
    fixture_message_registration,
)
from app.infrastructure.config import AppSettings
from app.infrastructure.runtime.event_loop import configure_asyncio_event_loop
from app.main import create_app
from run_sse_smoke import read_event, running_server, stream_headers
from test_database import temporary_migrated_schema

BASE = "/api/analysis/runs"


class GatedWorkflow:
    def __init__(self) -> None:
        self.calls: asyncio.Queue[tuple[WorkflowContext, asyncio.Event]] = asyncio.Queue()

    async def run(self, context: WorkflowContext) -> None:
        release = asyncio.Event()
        self.calls.put_nowait((context, release))
        await release.wait()
        context.cancellation.checkpoint()


class RejectingWorkflow:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, context: WorkflowContext) -> None:
        del context
        self.calls += 1
        raise AssertionError("stored runs must not restart workflow execution")


async def collect_stream(
    client: httpx.AsyncClient,
    path: str,
    connected: asyncio.Event,
) -> list[tuple[str, dict[str, object], int | None]]:
    events = []
    async with client.stream("GET", path) as response:
        stream_headers(response)
        connected.set()
        lines = response.aiter_lines()
        while True:
            event = await read_event(lines)
            events.append(event)
            if event[0] == "stream_end":
                assert await anext(lines, None) is None
                return events


async def wait_saved(application, run_id: UUID) -> None:
    coordinator = application.state.run_coordinator
    runtime = coordinator._runs[run_id]
    async with asyncio.timeout(15):
        async with runtime.condition:
            await runtime.condition.wait_for(
                lambda: coordinator._persistence_info_locked(runtime).state
                is PersistenceState.UP_TO_DATE
            )


def assert_request_id(response: httpx.Response) -> None:
    assert str(UUID(response.headers["x-request-id"])) == response.headers["x-request-id"]


async def create_saved_runs(settings: AppSettings) -> dict[str, object]:
    workflow = GatedWorkflow()
    registry = create_production_registry().copy_with(fixture_message_registration())
    app = create_app(
        settings,
        workflow=workflow,
        transcript_registry=registry,
        shutdown_grace_period=0,
        keepalive_interval=0.05,
    )
    async with running_server(app) as client:
        key = uuid4()
        query = "M3 跨平台 history smoke with UTF-8"
        created = await client.post(
            BASE,
            json={"query": query, "time_resolution": "6h"},
            headers={"Idempotency-Key": str(key)},
        )
        assert created.status_code == 201, created.text
        assert_request_id(created)
        receipt = created.json()
        completed_id = UUID(receipt["run_id"])
        _, release = await workflow.calls.get()
        coordinator = app.state.run_coordinator
        session = await coordinator.prepare_agent_session(
            completed_id,
            agent_role=AgentRole.QUERY_PARSER,
        )
        await coordinator.mark_agent_session_running(completed_id, session.session_id)

        run_connected = asyncio.Event()
        session_connected = asyncio.Event()
        run_events_task = asyncio.create_task(collect_stream(
            client,
            f"{BASE}/{completed_id}/events",
            run_connected,
        ))
        session_events_task = asyncio.create_task(collect_stream(
            client,
            f"{BASE}/{completed_id}/sessions/{session.session_id}/events",
            session_connected,
        ))
        await asyncio.gather(run_connected.wait(), session_connected.wait())

        large_text = "流域结论：" + ("water-quality-observation " * 180)
        message = await coordinator.start_transcript_message(
            completed_id,
            session.session_id,
            "fixture_message",
            1,
        )
        for offset in range(0, len(large_text), 97):
            await coordinator.append_transcript_message_delta(
                completed_id,
                message.item_id,
                large_text[offset:offset + 97],
            )
        await coordinator.finalize_transcript_message(
            completed_id,
            message.item_id,
            FixtureMessageV1(
                text=large_text,
                generation_status=GenerationStatus.COMPLETED,
            ),
        )
        await coordinator.complete_agent_session(completed_id, session.session_id)
        release.set()
        run_events, session_events = await asyncio.gather(
            run_events_task,
            session_events_task,
        )
        assert run_events[-1][0] == "stream_end"
        names = [name for name, _, _ in session_events]
        for expected in (
            "transcript_item",
            "message_started",
            "message_delta",
            "message_finalized",
            "stream_end",
        ):
            assert expected in names
        await wait_saved(app, completed_id)

        content = await client.get(
            f"{BASE}/{completed_id}/transcript/{message.item_id}/content",
            params={"view": "history"},
        )
        assert content.status_code == 200, content.text
        assert large_text in content.json()["text"]

        cancelled = await client.post(BASE, json={"query": "cancel smoke"})
        assert cancelled.status_code == 201, cancelled.text
        cancelled_id = UUID(cancelled.json()["run_id"])
        _, cancel_release = await workflow.calls.get()
        cancelled_response = await client.post(f"{BASE}/{cancelled_id}/cancel")
        assert cancelled_response.status_code == 200, cancelled_response.text
        assert cancelled_response.json()["status"] == "cancelled"
        cancel_release.set()
        cancelled_runtime = coordinator._runs[cancelled_id]
        await cancelled_runtime.workflow_quiesced.wait()
        await wait_saved(app, cancelled_id)

        interrupted = await client.post(BASE, json={"query": "restart repair smoke"})
        assert interrupted.status_code == 201, interrupted.text
        interrupted_id = UUID(interrupted.json()["run_id"])
        await workflow.calls.get()
        interrupted_session = await coordinator.prepare_agent_session(
            interrupted_id,
            agent_role=AgentRole.WATERBODY,
            target_comid="987654321",
        )
        await coordinator.mark_agent_session_running(
            interrupted_id,
            interrupted_session.session_id,
        )
        await wait_saved(app, interrupted_id)

    return {
        "key": key,
        "query": query,
        "receipt": receipt,
        "completed_id": completed_id,
        "session_id": session.session_id,
        "message_id": message.item_id,
        "large_text": large_text,
        "cancelled_id": cancelled_id,
        "interrupted_id": interrupted_id,
        "interrupted_session_id": interrupted_session.session_id,
    }


async def verify_restart(settings: AppSettings, state: dict[str, object]) -> None:
    workflow = RejectingWorkflow()
    registry = create_production_registry().copy_with(fixture_message_registration())
    app = create_app(
        settings,
        workflow=workflow,
        transcript_registry=registry,
        shutdown_grace_period=0,
        keepalive_interval=0.05,
    )
    async with running_server(app) as client:
        first_page = await client.get(BASE, params={"limit": 2})
        assert first_page.status_code == 200, first_page.text
        cursor = first_page.json()["next_cursor"]
        assert cursor
        second_page = await client.get(BASE, params={"limit": 2, "cursor": cursor})
        assert second_page.status_code == 200, second_page.text
        listed = {
            UUID(item["run_id"])
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
        detail = await client.get(f"{BASE}/{completed_id}", params={"view": "history"})
        assert detail.status_code == 200, detail.text
        assert detail.json()["source"] == "postgresql"
        assert detail.json()["run"]["query"] == state["query"]
        session = await client.get(
            f"{BASE}/{completed_id}/sessions/{session_id}/transcript",
            params={"view": "history"},
        )
        assert session.status_code == 200, session.text
        sequences = [item["transcript_sequence"] for item in session.json()["items"]]
        assert sequences == sorted(sequences)
        assert str(message_id) in {item["item_id"] for item in session.json()["items"]}
        content = await client.get(
            f"{BASE}/{completed_id}/transcript/{message_id}/content",
            params={"view": "history"},
        )
        assert content.status_code == 200, content.text
        assert state["large_text"] in content.json()["text"]

        interrupted_id = state["interrupted_id"]
        repaired = await client.get(
            f"{BASE}/{interrupted_id}",
            params={"view": "history"},
        )
        assert repaired.status_code == 200, repaired.text
        assert repaired.json()["run"]["status"] == "failed"
        repaired_session = await client.get(
            f"{BASE}/{interrupted_id}/sessions/{state['interrupted_session_id']}/transcript",
            params={"view": "history"},
        )
        assert repaired_session.status_code == 200, repaired_session.text
        repair_evidence = json.dumps(
            (repaired.json(), repaired_session.json()),
            ensure_ascii=False,
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
            json={"query": state["query"], "time_resolution": "6h"},
            headers={"Idempotency-Key": str(state["key"])},
        )
        assert repeated.status_code == 201, repeated.text
        assert repeated.json() == state["receipt"]
        assert workflow.calls == 0


async def run_smoke(settings: AppSettings) -> None:
    async with asyncio.timeout(60):
        state = await create_saved_runs(settings)
        await verify_restart(settings, state)
    print(json.dumps(
        {
            "history": "passed",
            "restart_repair": "passed",
            "database": settings.db_name,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ))


def main() -> None:
    with temporary_migrated_schema() as settings:
        asyncio.run(run_smoke(settings))


if __name__ == "__main__":
    configure_asyncio_event_loop()
    main()
