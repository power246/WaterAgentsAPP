import asyncio
import base64
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest

from app.application.analysis_runs.persistence import PersistenceState
from app.application.analysis_runs.models import RunStatus
from app.application.analysis_runs.workflow import WorkflowContext
from app.application.transcripts.models import (
    LateMessageOutputFact,
    MessageEndReason,
    WorkflowQuiescedFact,
)
from app.infrastructure.config import AppSettings
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from app.ports.errors import PortUnavailableError
from tests.m4_support import (
    analysis_context,
    create_request,
    create_test_app as create_app,
)

BASE = "/api/analysis/runs"


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 14, 1, 2, 3, 456789, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value


class GatedWorkflow:
    def __init__(self) -> None:
        self.started: asyncio.Queue[tuple[WorkflowContext, asyncio.Event]] = asyncio.Queue()

    async def run(self, context: WorkflowContext) -> None:
        release = asyncio.Event()
        self.started.put_nowait((context, release))
        await release.wait()
        context.cancellation.checkpoint()


class ReadFailureStore(InMemoryTranscriptStore):
    async def list_runs(self, limit, after_created_at, after_run_id):
        raise PortUnavailableError("database unavailable")

    async def read_run(self, run_id):
        raise PortUnavailableError("database unavailable")


class BlockingWriteStore(InMemoryTranscriptStore):
    def __init__(self) -> None:
        super().__init__()
        self.write_started = asyncio.Event()
        self.allow_write = asyncio.Event()

    async def write_batch(self, batch):
        self.write_started.set()
        await self.allow_write.wait()
        await super().write_batch(batch)


@asynccontextmanager
async def app_client(*, store=None, workflow=None, clock=None, settings=None):
    application = create_app(
        settings or AppSettings(_env_file=None),
        transcript_store=store or InMemoryTranscriptStore(),
        workflow=workflow,
        clock=clock,
        shutdown_grace_period=0,
    )
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            yield application, client


async def wait_saved(application, run_id: UUID) -> None:
    for _ in range(200):
        persistence = await application.state.run_coordinator.get_persistence_info(
            run_id
        )
        if persistence.state is not PersistenceState.PENDING:
            assert persistence.state is PersistenceState.UP_TO_DATE
            return
        await asyncio.sleep(0)
    raise AssertionError("persistence writer did not settle")


def run(exercise) -> None:
    asyncio.run(asyncio.wait_for(exercise(), timeout=10))


def encode_cursor_payload(payload: object) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")


def test_live_and_history_share_complete_reader_models_and_visibility() -> None:
    async def exercise() -> None:
        store = InMemoryTranscriptStore()
        workflow = GatedWorkflow()
        settings = AppSettings(
            _env_file=None,
            transcript_inline_max_bytes=256,
            transcript_preview_bytes=20,
        )
        async with app_client(
            store=store,
            workflow=workflow,
            settings=settings,
        ) as (application, client):
            created = await client.post(BASE, json=create_request("  inspect river  "))
            run_id = UUID(created.json()["run_id"])
            _, release = await workflow.started.get()
            coordinator = application.state.run_coordinator
            session = await coordinator.prepare_agent_session(
                run_id,
                agent_role="query_parser",
            )
            visible = await coordinator.reserve_transcript_item(
                run_id,
                session_id=session.session_id,
            )
            await coordinator.finalize_transcript_item(
                run_id,
                visible.item_id,
                "runtime_fact",
                1,
                WorkflowQuiescedFact(),
            )
            request_item_id = coordinator._runs[run_id].transcript_items[0].item_id
            audit = await coordinator.reserve_transcript_item(
                run_id,
                session_id=session.session_id,
            )
            await coordinator.finalize_transcript_item(
                run_id,
                audit.item_id,
                "runtime_fact",
                1,
                LateMessageOutputFact(
                    original_item_id=request_item_id,
                    text="水" * 400,
                    end_reason=MessageEndReason.COMPLETED,
                ),
            )
            await wait_saved(application, run_id)

            detail = await client.get(f"{BASE}/{run_id}")
            assert detail.status_code == 200
            assert detail.json()["source"] == "live"
            assert detail.json()["run"]["query"] == "inspect river"
            assert detail.json()["run"]["configuration"][
                "time_resolution_preference"
            ] == "12h"
            assert detail.json()["sessions"] == [
                {
                    "session_id": str(session.session_id),
                    "agent_role": "query_parser",
                    "target_comid": None,
                    "status": "pending",
                    "created_at": detail.json()["sessions"][0]["created_at"],
                    "updated_at": detail.json()["sessions"][0]["updated_at"],
                    "result_ref": None,
                    "result_summary": None,
                }
            ]
            assert all(item["scope"]["session_id"] is None for item in detail.json()["items"])

            session_detail = await client.get(
                f"{BASE}/{run_id}/sessions/{session.session_id}?view=history"
            )
            assert session_detail.status_code == 200
            assert session_detail.json()["source"] == "postgresql"
            assert session_detail.json()["persistence"] is None
            assert session_detail.json()["transcript_url"].endswith("?view=history")

            current = await client.get(
                f"{BASE}/{run_id}/sessions/{session.session_id}/transcript"
            )
            history = await client.get(
                f"{BASE}/{run_id}/sessions/{session.session_id}/transcript?view=history"
            )
            assert current.status_code == history.status_code == 200
            assert current.json()["source"] == "live"
            assert history.json()["source"] == "postgresql"
            assert str(visible.item_id) in {
                item["item_id"] for item in current.json()["items"]
            }
            assert str(audit.item_id) not in {
                item["item_id"] for item in current.json()["items"]
            }
            assert str(audit.item_id) in {
                item["item_id"] for item in history.json()["items"]
            }
            assert [
                item["item_id"] for item in history.json()["referenced_items"]
            ] == [str(request_item_id)]
            sequences = [item["transcript_sequence"] for item in history.json()["items"]]
            assert sequences == sorted(sequences)

            audit_view = next(
                item
                for item in history.json()["items"]
                if item["item_id"] == str(audit.item_id)
            )
            assert audit_view["content"]["kind"] == "reference"
            assert audit_view["content"]["truncated"] is True
            assert audit_view["content_bytes"] > 256
            assert (
                await client.get(f"{BASE}/{run_id}/transcript/{audit.item_id}/content")
            ).status_code == 404
            content = await client.get(
                f"{BASE}/{run_id}/transcript/{audit.item_id}/content?view=history"
            )
            assert content.status_code == 200
            assert content.json()["source"] == "postgresql"
            assert json.loads(content.json()["text"])["text"] == "水" * 400
            assert len(content.json()["text"].encode()) == content.json()["content_bytes"]

            other = await client.post(BASE, json=create_request("other run"))
            cross_run = await client.get(
                f"{BASE}/{other.json()['run_id']}/transcript/{audit.item_id}/content"
            )
            assert cross_run.status_code == 404
            assert cross_run.json()["error"]["code"] == "transcript_item_not_found"
            missing_session = await client.get(
                f"{BASE}/{run_id}/sessions/{uuid4()}"
            )
            assert missing_session.status_code == 404
            assert missing_session.json()["error"]["code"] == "session_not_found"
            release.set()
            runtime = coordinator._runs[run_id]
            await runtime.workflow_quiesced.wait()
            await wait_saved(application, run_id)
            coordinator._runs.pop(run_id)
            fallback = await client.get(
                f"{BASE}/{run_id}/transcript/{audit.item_id}/content"
            )
            assert fallback.status_code == 404
            coordinator._runs[run_id] = runtime

    run(exercise)


@pytest.mark.parametrize("damage", ["unknown", "corrupt"])
def test_context_summary_and_original_reference_survive_reload_and_report_damage(
    damage: str,
) -> None:
    async def exercise() -> None:
        store = InMemoryTranscriptStore()
        workflow = GatedWorkflow()
        settings = AppSettings(
            _env_file=None,
            transcript_inline_max_bytes=256,
            transcript_preview_bytes=20,
        )
        async with app_client(
            store=store,
            workflow=workflow,
            settings=settings,
        ) as (application, client):
            request = create_request("Will dissolved oxygen become unsafe?")
            created = await client.post(BASE, json=request)
            run_id = UUID(created.json()["run_id"])
            _, release = await workflow.started.get()
            coordinator = application.state.run_coordinator
            session = await coordinator.prepare_agent_session(
                run_id,
                agent_role="query_parser",
            )
            await coordinator.mark_agent_session_running(run_id, session.session_id)
            context_item = await coordinator.reserve_transcript_item(
                run_id,
                session_id=session.session_id,
            )
            await coordinator.finalize_transcript_item(
                run_id,
                context_item.item_id,
                "query_understood",
                1,
                analysis_context(request["query"]),
            )
            await wait_saved(application, run_id)

            listed = (await client.get(BASE)).json()["items"][0]
            summary = listed["context_summary"]
            assert summary["target_comid"] == "12345678"
            assert summary["params_used"] == [{
                "code_system": "usgs",
                "code": "00300",
                "canonical_name": "dissolved oxygen",
                "source": "explicit",
                "rationale": "Dissolved oxygen is explicitly requested.",
            }]
            assert summary["supporting_params"] == [{
                "code_system": "usgs",
                "code": "00060",
                "canonical_name": "discharge",
            }]
            detail = (await client.get(f"{BASE}/{run_id}?view=history")).json()
            assert all(
                item["content_type"] != "query_understood"
                for item in detail["items"]
            )
            context_view = next(
                item
                for item in detail["referenced_items"]
                if item["content_type"] == "query_understood"
            )
            assert context_view["scope"]["agent_role"] == "query_parser"
            assert context_view["content"]["kind"] == "reference"
            full = await client.get(
                f"{BASE}/{run_id}/transcript/{context_item.item_id}/content?view=history"
            )
            assert full.status_code == 200
            assert json.loads(full.json()["text"])["target"]["comid"] == "12345678"

            stored_item = store._items[context_item.item_id]
            if damage == "unknown":
                store._items[context_item.item_id] = stored_item.model_copy(
                    update={"content_schema_version": 99}
                )
                expected_code = "transcript_content_unsupported"
            else:
                content = store._contents[context_item.item_id]
                store._contents[context_item.item_id] = content.model_copy(
                    update={"body": '{"schema_version":1}'}
                )
                expected_code = "transcript_content_corrupt"

            damaged = (await client.get(BASE)).json()["items"][0]
            assert damaged["context_summary"] is None
            assert {issue["error"]["code"] for issue in damaged["issues"]} == {
                expected_code
            }
            damaged_detail = (
                await client.get(f"{BASE}/{run_id}?view=history")
            ).json()
            assert damaged_detail["run"]["context_summary"] is None
            assert expected_code in {
                issue["error"]["code"] for issue in damaged_detail["issues"]
            }
            release.set()

    run(exercise)


def test_authorization_text_round_trips_through_history_and_full_content() -> None:
    async def exercise() -> None:
        store = InMemoryTranscriptStore()
        query = "Authorization: [REDACTED]"
        async with app_client(store=store) as (application, client):
            created = await client.post(BASE, json=create_request(query))
            assert created.status_code == 201
            run_id = UUID(created.json()["run_id"])
            await wait_saved(application, run_id)

            listing = await client.get(BASE)
            listed = next(
                item for item in listing.json()["items"]
                if item["run_id"] == str(run_id)
            )
            assert listed["query"] == query
            assert listed["issues"] == []

            detail = await client.get(f"{BASE}/{run_id}?view=history")
            request_item = next(
                item for item in detail.json()["items"]
                if item["content_type"] == "analysis_request"
            )
            assert request_item["parse_status"] == "ok"
            assert request_item["content"] == {
                "kind": "inline",
                "body": create_request(query),
            }
            assert detail.json()["issues"] == []

            content = await client.get(
                f"{BASE}/{run_id}/transcript/{request_item['item_id']}/content"
                "?view=history"
            )
            assert content.status_code == 200
            assert json.loads(content.json()["text"]) == create_request(query)
            assert request_item["content_bytes"] == content.json()["content_bytes"]
            assert len(content.json()["text"].encode("utf-8")) == content.json()[
                "content_bytes"
            ]

    run(exercise)


def test_query_at_sign_url_round_trips_without_userinfo_redaction() -> None:
    async def exercise() -> None:
        query = "https://example.com/path?contact=alice@example.net"
        async with app_client() as (application, client):
            created = await client.post(BASE, json=create_request(query))
            assert created.status_code == 201
            run_id = UUID(created.json()["run_id"])
            await wait_saved(application, run_id)

            detail = await client.get(f"{BASE}/{run_id}?view=history")
            assert detail.status_code == 200
            request_item = next(
                item for item in detail.json()["items"]
                if item["content_type"] == "analysis_request"
            )
            assert request_item["content"]["body"] == create_request(query)
            assert "message_content_mismatch" not in detail.text

    run(exercise)


def test_history_cursor_is_stable_strict_and_uses_uuid_tie_breaks() -> None:
    async def exercise() -> None:
        store = InMemoryTranscriptStore()
        clock = MutableClock()
        first_cursor = None
        expected = []
        async with app_client(store=store, clock=clock) as (_, client):
            run_ids = [
                UUID(
                    (
                        await client.post(
                            BASE, json=create_request(f"query {index}")
                        )
                    ).json()["run_id"]
                )
                for index in range(5)
            ]
            expected = sorted(run_ids, reverse=True)
            first = await client.get(BASE, params={"limit": 2})
            assert first.status_code == 200
            assert [UUID(item["run_id"]) for item in first.json()["items"]] == expected[:2]
            first_cursor = first.json()["next_cursor"]
            assert first_cursor

            changed = store._run_snapshots[expected[2]]
            store._run_snapshots[expected[2]] = changed.model_copy(update={
                "status": RunStatus.CANCELLED,
                "updated_at": changed.updated_at + timedelta(seconds=30),
            })
            clock.value += timedelta(seconds=1)
            newest = UUID(
                (await client.post(BASE, json=create_request("new page"))).json()[
                    "run_id"
                ]
            )
            second = await client.get(
                BASE,
                params={"limit": 2, "cursor": first_cursor},
            )
            assert [UUID(item["run_id"]) for item in second.json()["items"]] == expected[2:4]
            assert newest not in {UUID(item["run_id"]) for item in second.json()["items"]}
            last = await client.get(
                BASE,
                params={"limit": 2, "cursor": second.json()["next_cursor"]},
            )
            assert [UUID(item["run_id"]) for item in last.json()["items"]] == expected[4:]
            assert last.json()["next_cursor"] is None

        # A cursor is self-contained and remains valid across process lifespans.
        async with app_client(store=store, clock=clock) as (_, restarted_client):
            restarted = await restarted_client.get(
                BASE,
                params={"limit": 2, "cursor": first_cursor},
            )
            assert [UUID(item["run_id"]) for item in restarted.json()["items"]] == expected[2:4]

        empty_store = InMemoryTranscriptStore()
        async with app_client(store=empty_store, clock=clock) as (_, empty_client):
            assert (await empty_client.get(BASE)).json() == {
                "items": [],
                "next_cursor": None,
            }

    run(exercise)


def test_invalid_read_parameters_and_database_failures_are_explicit() -> None:
    async def exercise() -> None:
        store = ReadFailureStore()
        workflow = GatedWorkflow()
        async with app_client(store=store, workflow=workflow) as (application, client):
            created = await client.post(BASE, json=create_request("live query"))
            run_id = created.json()["run_id"]
            _, release = await workflow.started.get()
            assert (await client.get(f"{BASE}/{run_id}")).json()["source"] == "live"
            history = await client.get(f"{BASE}/{run_id}?view=history")
            assert history.status_code == 503
            assert history.json()["error"]["code"] == "database_unavailable"
            listing = await client.get(BASE)
            assert listing.status_code == 503
            assert listing.json()["error"]["code"] == "database_unavailable"

            invalid_requests = (
                (BASE + "?limit=0", "validation_error"),
                (BASE + "?limit=201", "validation_error"),
                (BASE + "?limit=no", "validation_error"),
                (BASE + "?cursor=invalid!", "invalid_history_cursor"),
                (
                    BASE + "?cursor=" + encode_cursor_payload({
                        "v": 2,
                        "created_at": "2026-09-14T01:02:03.456789Z",
                        "run_id": str(uuid4()),
                    }),
                    "invalid_history_cursor",
                ),
                (
                    BASE + "?cursor=" + encode_cursor_payload({
                        "v": 1,
                        "created_at": "2026-09-14T01:02:03Z",
                        "run_id": str(uuid4()),
                    }),
                    "invalid_history_cursor",
                ),
                (BASE + "?cursor=" + "a" * 1025, "invalid_history_cursor"),
                (BASE + "?status=running", "validation_error"),
                (f"{BASE}/{run_id}?view=current", "validation_error"),
                (
                    f"{BASE}/{run_id}/sessions/{uuid4()}/transcript?limit=1",
                    "validation_error",
                ),
            )
            for path, code in invalid_requests:
                response = await client.get(path)
                assert response.status_code == 422
                assert response.json()["error"]["code"] == code
                assert set(response.json()) == {
                    "error", "run", "validation", "history_url"
                }
            runtime = application.state.run_coordinator._runs.pop(UUID(run_id))
            retry = await client.post(f"{BASE}/{run_id}/persistence/retry")
            assert retry.status_code == 503
            assert retry.json()["error"]["code"] == "database_unavailable"
            application.state.run_coordinator._runs[UUID(run_id)] = runtime
            release.set()

    run(exercise)


def test_history_never_mixes_unpersisted_live_items_into_postgres_view() -> None:
    async def exercise() -> None:
        store = BlockingWriteStore()
        workflow = GatedWorkflow()
        async with app_client(store=store, workflow=workflow) as (application, client):
            created = await client.post(BASE, json=create_request("partial history"))
            run_id = UUID(created.json()["run_id"])
            _, release = await workflow.started.get()
            await store.write_started.wait()
            session = await application.state.run_coordinator.prepare_agent_session(
                run_id,
                agent_role="query_parser",
            )
            item = await application.state.run_coordinator.reserve_transcript_item(
                run_id,
                session_id=session.session_id,
            )
            await application.state.run_coordinator.finalize_transcript_item(
                run_id,
                item.item_id,
                "runtime_fact",
                1,
                WorkflowQuiescedFact(),
            )

            live = await client.get(f"{BASE}/{run_id}")
            history = await client.get(f"{BASE}/{run_id}?view=history")
            assert live.status_code == history.status_code == 200
            assert live.json()["source"] == "live"
            assert live.json()["sessions"][0]["session_id"] == str(session.session_id)
            assert history.json()["source"] == "postgresql"
            assert history.json()["sessions"] == []
            assert str(item.item_id) not in {
                entry["item_id"] for entry in history.json()["items"]
            }

            store.allow_write.set()
            release.set()

    run(exercise)


def test_corrupt_items_keep_safe_fulltext_unless_body_is_unreadable(
    application_logs: list[str],
) -> None:
    async def exercise() -> None:
        store = InMemoryTranscriptStore()
        settings = AppSettings(
            _env_file=None,
            transcript_inline_max_bytes=256,
            transcript_preview_bytes=20,
        )
        async with app_client(store=store, settings=settings) as (_, client):
            created = await client.post(BASE, json=create_request("corrupt config"))
            run_id = UUID(created.json()["run_id"])
            candidate = store._runs[run_id]
            item = store._items[candidate.created_item_id]
            unsafe_values = (
                "nested-secret",
                "dXNlcjpwYXNz",
                "private-secret",
            )
            unsafe_body = json.dumps(
                {
                    "type": "run_created",
                    "payload": {
                        "configuration": 7,
                        "nested": {
                            "api_key": unsafe_values[0],
                            "note": f"Authorization: Basic {unsafe_values[1]}",
                        },
                        "private_key": unsafe_values[2],
                    },
                },
                indent=2,
            )
            store._items[item.item_id] = item.model_copy(update={
                "content_bytes": len(unsafe_body.encode("utf-8")),
            })
            stored_content = store._contents[item.item_id]
            store._contents[item.item_id] = stored_content.model_copy(
                update={"body": unsafe_body}
            )

            listing = await client.get(BASE)
            listed = next(
                item for item in listing.json()["items"]
                if item["run_id"] == str(run_id)
            )
            assert listed["configuration"] is None
            assert listed["issues"][0]["error"]["code"] == "transcript_content_corrupt"
            detail = await client.get(f"{BASE}/{run_id}?view=history")
            corrupt = next(
                item
                for item in detail.json()["items"]
                if item["item_id"] == str(candidate.created_item_id)
            )
            assert corrupt["parse_status"] == "corrupt"
            assert corrupt["content"] == {"kind": "unparsed"}
            assert all(secret not in detail.text for secret in unsafe_values)
            content_response = await client.get(
                f"{BASE}/{run_id}/transcript/{candidate.created_item_id}/content?view=history"
            )
            assert content_response.status_code == 200
            safe_text = content_response.json()["text"]
            assert json.loads(safe_text) == {
                "type": "run_created",
                "payload": {
                    "configuration": 7,
                    "nested": {
                        "api_key": "[REDACTED]",
                        "note": "Authorization: [REDACTED]",
                    },
                    "private_key": "[REDACTED]",
                },
            }
            assert content_response.json()["issues"][0]["error"]["code"] == (
                "transcript_content_corrupt"
            )
            assert content_response.json()["content_bytes"] == len(
                safe_text.encode("utf-8")
            )
            assert all(secret not in content_response.text for secret in unsafe_values)
            assert all(secret not in "".join(application_logs) for secret in unsafe_values)

            store._items[item.item_id] = item.model_copy(
                update={"content_bytes": 1}
            )
            store._contents[item.item_id] = stored_content
            mismatched = await client.get(
                f"{BASE}/{run_id}/transcript/{item.item_id}/content?view=history"
            )
            assert mismatched.status_code == 200
            assert json.loads(mismatched.json()["text"])["type"] == "run_created"
            assert mismatched.json()["issues"][0]["error"]["code"] == (
                "transcript_content_corrupt"
            )
            assert mismatched.json()["content_bytes"] == len(
                mismatched.json()["text"].encode("utf-8")
            )

            store._contents[item.item_id] = stored_content.model_copy(
                update={"body": "{"}
            )
            malformed = await client.get(
                f"{BASE}/{run_id}/transcript/{item.item_id}/content?view=history"
            )
            assert malformed.status_code == 500
            assert malformed.json()["error"]["code"] == (
                "transcript_content_unreadable"
            )

            store._contents.pop(item.item_id)
            unreadable = await client.get(
                f"{BASE}/{run_id}/transcript/{item.item_id}/content?view=history"
            )
            assert unreadable.status_code == 500
            assert unreadable.json()["error"]["code"] == "transcript_content_unreadable"

    run(exercise)


@pytest.mark.parametrize("body", ('{"invalid":true}', '{"invalid": true}'))
def test_invalid_registered_body_is_isolated_regardless_of_json_spacing(body: str) -> None:
    async def exercise() -> None:
        store = InMemoryTranscriptStore()
        async with app_client(store=store) as (_, client):
            created = await client.post(BASE, json=create_request("spacing check"))
            run_id = UUID(created.json()["run_id"])
            request_id = store._runs[run_id].request_item_id
            item = store._items[request_id]
            store._items[request_id] = item.model_copy(update={
                "content_type": "waterbody_model_response",
                "content_schema_version": 1,
                "inline_body": body,
                "content_bytes": len(body.encode("utf-8")),
            })
            store._contents.pop(request_id, None)
            response = await client.get(f"{BASE}/{run_id}?view=history")
            assert response.status_code == 200
            payload = response.json()
            damaged = next(
                item for item in payload["items"]
                if item["item_id"] == str(request_id)
            )
            assert damaged["parse_status"] == "corrupt"
            assert damaged["content"] == {"kind": "unparsed"}
            assert any(
                issue["item_id"] == str(request_id)
                and issue["error"]["code"] == "transcript_content_corrupt"
                for issue in payload["issues"]
            )
            assert any(item["item_id"] != str(request_id) for item in payload["items"])

    run(exercise)


@pytest.mark.parametrize("content_type", (
    "run_summary_model_request",
    "run_summary_model_response",
))
@pytest.mark.parametrize("version,issue_code", (
    (1, "transcript_content_corrupt"),
    (999, "transcript_content_unsupported"),
))
def test_invalid_summary_fulltext_uses_public_projection(
    content_type: str, version: int, issue_code: str,
) -> None:
    async def exercise() -> None:
        store = InMemoryTranscriptStore()
        async with app_client(store=store) as (application, client):
            created = await client.post(BASE, json=create_request("summary projection"))
            run_id = UUID(created.json()["run_id"])
            await wait_saved(application, run_id)
            request_id = store._runs[run_id].request_item_id
            item = store._items[request_id]
            private = "PRIVATE_SUMMARY_BODY_MARKER"
            body = json.dumps({
                "schema_version": version,
                "operation_id": str(uuid4()),
                "invalid": True,
                "system_prompt": private,
                "messages": [{"text": private}],
                "structured_output": private,
                "visible_text": private,
            })
            store._items[request_id] = item.model_copy(update={
                "content_type": content_type,
                "content_schema_version": version,
                "inline_body": body,
                "content_bytes": len(body.encode("utf-8")),
            })
            store._contents.pop(request_id, None)

            detail = await client.get(f"{BASE}/{run_id}?view=history")
            assert detail.status_code == 200
            assert private not in detail.text
            assert any(
                issue["item_id"] == str(request_id)
                and issue["error"]["code"] == issue_code
                for issue in detail.json()["issues"]
            )

            content = await client.get(
                f"{BASE}/{run_id}/transcript/{request_id}/content?view=history"
            )
            assert content.status_code == 200
            assert private not in content.text
            assert any(
                issue["item_id"] == str(request_id)
                and issue["error"]["code"] == issue_code
                for issue in content.json()["issues"]
            )
            public = json.loads(content.json()["text"])
            assert public["schema_version"] == version
            assert UUID(public["operation_id"])
            if content_type == "run_summary_model_response":
                assert public["structured_output_present"] is True
                assert public["visible_text_present"] is True
            else:
                assert "structured_output_present" not in public

    run(exercise)


def test_unsupported_fulltext_requires_strict_history_view() -> None:
    async def exercise() -> None:
        store = InMemoryTranscriptStore()
        workflow = GatedWorkflow()
        async with app_client(store=store, workflow=workflow) as (application, client):
            created = await client.post(BASE, json=create_request("future content"))
            run_id = UUID(created.json()["run_id"])
            _, release = await workflow.started.get()
            await wait_saved(application, run_id)
            candidate = store._runs[run_id]
            item = store._items[candidate.request_item_id]
            store._items[item.item_id] = item.model_copy(update={
                "content_type": "future_content",
            })

            coordinator = application.state.run_coordinator
            runtime = coordinator._runs.pop(run_id)
            try:
                auto = await client.get(
                    f"{BASE}/{run_id}/transcript/{item.item_id}/content"
                )
                assert auto.status_code == 404
                assert auto.json()["error"]["code"] == "transcript_item_not_found"

                history = await client.get(
                    f"{BASE}/{run_id}/transcript/{item.item_id}/content?view=history"
                )
                assert history.status_code == 200
                assert history.json()["content_type"] == "future_content"
                assert json.loads(history.json()["text"]) == create_request(
                    "future content"
                )
                assert history.json()["issues"][0]["error"]["code"] == (
                    "transcript_content_unsupported"
                )
            finally:
                coordinator._runs[run_id] = runtime
                release.set()

    run(exercise)


def test_cancel_and_manual_persistence_controls_cover_live_and_history(
    monkeypatch,
) -> None:
    async def exercise() -> None:
        store = InMemoryTranscriptStore()
        workflow = GatedWorkflow()
        async with app_client(
            store=store,
            workflow=workflow,
            settings=AppSettings(
                _env_file=None,
                live_state_grace_seconds=3600,
            ),
        ) as (application, client):
            created = await client.post(BASE, json=create_request("control run"))
            run_id = UUID(created.json()["run_id"])
            _, release = await workflow.started.get()
            await wait_saved(application, run_id)
            retry = await client.post(f"{BASE}/{run_id}/persistence/retry")
            assert retry.status_code == 200
            assert retry.json()["persistence"]["state"] == "up_to_date"

            draft = await application.state.run_coordinator.reserve_transcript_item(run_id)
            pending = await client.post(f"{BASE}/{run_id}/persistence/retry")
            assert pending.status_code == 202
            assert pending.json()["persistence"]["state"] == "pending"
            await application.state.run_coordinator.finalize_transcript_item(
                run_id,
                draft.item_id,
                "runtime_fact",
                1,
                WorkflowQuiescedFact(),
            )
            release.set()
            runtime = application.state.run_coordinator._runs[run_id]
            await runtime.workflow_quiesced.wait()
            await wait_saved(application, run_id)

            coordinator = application.state.run_coordinator
            assert runtime.cleanup_task is not None
            runtime.cleanup_task.cancel()
            await asyncio.gather(runtime.cleanup_task, return_exceptions=True)
            runtime.cleanup_eligible_at = (
                asyncio.get_running_loop().time()
                - coordinator._live_state_grace_period
                - 1
            )
            original_retry = coordinator.retry_persistence
            retry_entered = asyncio.Event()
            allow_retry = asyncio.Event()
            retry_calls = 0

            async def gated_retry(candidate_run_id):
                nonlocal retry_calls
                retry_calls += 1
                retry_entered.set()
                await allow_retry.wait()
                return await original_retry(candidate_run_id)

            monkeypatch.setattr(coordinator, "retry_persistence", gated_retry)
            unavailable_task = asyncio.create_task(
                client.post(f"{BASE}/{run_id}/persistence/retry")
            )
            await retry_entered.wait()
            assert await coordinator._release_live_state(runtime) is True
            allow_retry.set()
            unavailable = await unavailable_task

            assert retry_calls == 1
            assert unavailable.status_code == 409
            assert unavailable.json()["error"]["code"] == "persistence_retry_unavailable"
            stored = await store.read_run(run_id)
            assert stored is not None
            assert not any(
                item.content_type == "runtime_fact"
                and item.inline_body is not None
                and json.loads(item.inline_body)["fact"] == "persistence_recovered"
                for item in stored.items
            )
            terminal = await client.post(f"{BASE}/{run_id}/cancel")
            assert terminal.status_code == 409
            assert terminal.json()["error"]["code"] == "run_already_terminal"
            assert terminal.json()["run"]["status"] == "completed"
            assert terminal.json()["run"]["persistence"] is None
            missing = await client.post(f"{BASE}/{uuid4()}/persistence/retry")
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "run_not_found"

        cancelled_store = InMemoryTranscriptStore()
        cancelled_workflow = GatedWorkflow()
        async with app_client(
            store=cancelled_store,
            workflow=cancelled_workflow,
        ) as (application, client):
            created = await client.post(BASE, json=create_request("cancelled history"))
            run_id = UUID(created.json()["run_id"])
            _, release = await cancelled_workflow.started.get()
            cancelled = await client.post(f"{BASE}/{run_id}/cancel")
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"
            runtime = application.state.run_coordinator._runs[run_id]
            release.set()
            await runtime.workflow_quiesced.wait()
            await wait_saved(application, run_id)
            application.state.run_coordinator._runs.pop(run_id)
            repeated = await client.post(f"{BASE}/{run_id}/cancel")
            assert repeated.status_code == 200
            assert repeated.json()["status"] == "cancelled"
            assert repeated.json()["persistence"] is None
            application.state.run_coordinator._runs[run_id] = runtime

    run(exercise)
