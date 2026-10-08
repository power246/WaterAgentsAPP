import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest

from tests.application.coordinator_inspection import set_waterbody_participation

from app.api.schemas.analysis_runs import CreateAnalysisRunRequest
from app.api.sse.encoder import KEEPALIVE_FRAME, encode_event
from app.api.sse.response import (
    LiveStreamReleasedError,
    create_run_event_response,
    create_session_event_response,
)
from app.application.analysis_runs.coordinator import (
    EventCursorAheadError,
    InvalidEventCursorError,
    RunCoordinator,
    RunNotFoundError,
)
from app.application.analysis_runs.models import AgentRole, RunStatus
from app.application.analysis_runs.workflow import WorkflowContext
from app.application.transcripts.models import GenerationStatus
from app.application.transcripts.registry import create_production_registry
from app.application.transcripts.stream_models import (
    MessageDeltaProcessRecord,
    MessageFinalizedProcessRecord,
    MessageStartedProcessRecord,
    RunSnapshotEvent,
    TranscriptItemProcessRecord,
)
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from app.infrastructure.config import AppSettings
from tests.m4_support import (
    MODEL_CATALOG,
    NoOpWorkflow,
    create_request,
    create_test_app as create_app,
)
from scripts.transcript_fixtures import (
    FixtureMessageV1,
    fixture_message_registration,
)

CONFIGURATION = CreateAnalysisRunRequest.model_validate(
    create_request("query")
).to_configuration_snapshot("America/Chicago")


class TickingClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 13, 3, 20, tzinfo=UTC)

    def now(self) -> datetime:
        value = self.value
        self.value += timedelta(microseconds=1)
        return value


class GatedWorkflow:
    def __init__(self) -> None:
        self.calls: asyncio.Queue[tuple[WorkflowContext, asyncio.Event]] = asyncio.Queue()

    async def run(self, context: WorkflowContext) -> None:
        release = asyncio.Event()
        self.calls.put_nowait((context, release))
        await release.wait()
        context.cancellation.checkpoint()


@asynccontextmanager
async def run_runtime(*, message_registry: bool = False, **options):
    workflow = GatedWorkflow()
    store = options.pop("transcript_store", InMemoryTranscriptStore())
    registry = create_production_registry()
    if message_registry:
        registry = registry.copy_with(fixture_message_registration())
    application = create_app(
        clock=TickingClock(),
        workflow=workflow,
        transcript_store=store,
        transcript_registry=registry,
        shutdown_grace_period=0,
        **options,
    )
    async with application.router.lifespan_context(application):
        coordinator = application.state.run_coordinator
        created = await coordinator.create_run("private query", CONFIGURATION)
        context, release = await workflow.calls.get()
        yield application, coordinator, created, context, release, store


def run(exercise) -> None:
    asyncio.run(asyncio.wait_for(exercise(), timeout=15))


async def collect_response(response):
    messages = []

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    return messages


def frames(messages) -> list[str]:
    return [
        message["body"].decode("utf-8")
        for message in messages
        if message.get("body")
    ]


def decoded_frames(messages) -> list[tuple[str, int | None, dict[str, object]]]:
    decoded = []
    for frame in frames(messages):
        lines = frame.splitlines()
        event_id = int(lines[0][4:]) if lines[0].startswith("id: ") else None
        offset = 1 if event_id is not None else 0
        decoded.append((
            lines[offset].removeprefix("event: "),
            event_id,
            json.loads(lines[offset + 1].removeprefix("data: ")),
        ))
    return decoded


def test_encoder_uses_optional_integer_id_and_one_json_data_line() -> None:
    event = RunSnapshotEvent(
        run_id=UUID("10ef14bd-d0e7-4a70-9728-1fbb4ca7a83c"),
        status=RunStatus.RUNNING,
        analysis_phase="query_understanding",
        created_at="2026-09-13T03:20:00Z",
        updated_at="2026-09-13T03:20:00.000001Z",
        context_ref=None,
        topology_ref=None,
        result_ref=None,
        summary={"status": "not_started"},
        graph_outcome={},
        persistence={"state": "pending", "warning": None},
    )
    without_id = encode_event("run_snapshot", event)
    with_id = encode_event("fixture", event, event_id=3)
    assert without_id.startswith(b"event: run_snapshot\ndata: {")
    assert b"\nid:" not in without_id
    assert with_id.startswith(b"id: 3\nevent: fixture\ndata: {")
    assert without_id.count(b"\n") == 3
    assert KEEPALIVE_FRAME == b": keepalive\n\n"
    json.loads(without_id.decode().splitlines()[1].removeprefix("data: "))


def test_run_subscription_assembles_latest_real_state_without_notification_log() -> None:
    async def exercise() -> None:
        async with run_runtime() as (_, coordinator, created, _, _, _):
            await set_waterbody_participation(coordinator,
                created.run_id,
                {"101": False, "202": True},
            )
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.WATERBODY,
                target_comid="202",
            )
            lifecycle_events = tuple(coordinator._runs[created.run_id].events)
            stream = await coordinator.subscribe_run(created.run_id)
            snapshot = await anext(stream)
            await stream.aclose()
            assert snapshot.sessions == (session,)
            assert [item.model_dump(mode="json") for item in snapshot.waterbodies] == [
                {
                    "target_comid": "101",
                    "participating": False,
                    "scheduling_status": "excluded",
                    "dependency_status": None,
                    "session_id": None,
                    "session_status": None,
                    "result_ref": None,
                },
                {
                    "target_comid": "202",
                    "participating": True,
                    "scheduling_status": "running",
                    "dependency_status": None,
                    "session_id": str(session.session_id),
                    "session_status": "pending",
                    "result_ref": None,
                },
            ]
            assert tuple(coordinator._runs[created.run_id].events) == lifecycle_events
            assert "private query" not in snapshot.model_dump_json()

    run(exercise)


def test_run_reconnect_starts_with_one_current_snapshot_and_ignores_header_at_http() -> None:
    async def exercise() -> None:
        async with run_runtime() as (app, coordinator, created, _, _, _):
            first = await coordinator.subscribe_run(created.run_id)
            await anext(first)
            await first.aclose()
            await set_waterbody_participation(coordinator, created.run_id, {"303": True})
            reconnect = await coordinator.subscribe_run(created.run_id)
            latest = await anext(reconnect)
            await reconnect.aclose()
            assert [item.target_comid for item in latest.waterbodies] == ["303"]
            operation = app.openapi()["paths"][f"/api/analysis/runs/{{run_id}}/events"]["get"]
            assert all(item["name"] != "Last-Event-ID" for item in operation["parameters"])

    run(exercise)


def test_session_log_exists_without_subscribers_and_preserves_phase_order() -> None:
    async def exercise() -> None:
        async with run_runtime(message_registry=True) as (
            _, coordinator, created, _, _, store,
        ):
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            await coordinator.mark_agent_session_running(created.run_id, session.session_id)
            message = await coordinator.start_transcript_message(
                created.run_id,
                session.session_id,
                "fixture_message",
                1,
            )
            await coordinator.append_transcript_message_delta(
                created.run_id,
                message.item_id,
                "first ",
            )
            await coordinator.finalize_transcript_message(
                created.run_id,
                message.item_id,
                FixtureMessageV1(
                    text="first answer",
                    generation_status=GenerationStatus.COMPLETED,
                ),
            )
            await coordinator.complete_agent_session(created.run_id, session.session_id)
            records = coordinator._runs[created.run_id].agent_sessions[
                session.session_id
            ].process_events
            assert [type(item) for item in records] == [
                TranscriptItemProcessRecord,
                MessageStartedProcessRecord,
                MessageDeltaProcessRecord,
                MessageDeltaProcessRecord,
                MessageFinalizedProcessRecord,
                TranscriptItemProcessRecord,
            ]
            assert [item.sequence for item in records] == [1, 2, 3, 4, 5, 6]
            assert (
                records[1].header.item_id
                == records[2].item_id
                == records[3].item_id
                == records[4].item.item_id
            )
            assert records[1].header.transcript_sequence == message.transcript_sequence
            assert records[1].header.occurred_at == message.created_at
            assert records[2].text + records[3].text == "first answer"
            await coordinator._transcript_writer.drain(
                coordinator._runs[created.run_id]
            )
            stored = await store.read_run(created.run_id)
            assert sum(item.item_id == message.item_id for item in stored.items) == 1
            assert all(
                item.content_type not in {
                    "transcript_item",
                    "message_started",
                    "message_delta",
                    "message_finalized",
                    "stream_end",
                    "run_snapshot",
                }
                for item in stored.items
            )

    run(exercise)


@pytest.mark.parametrize(
    "cursor",
    ["", "-1", "+1", " 1", "1 ", "1\n", "1.0", "1e0", "0x1", "１", "١", "1_0", "a"],
)
def test_session_cursor_rejects_non_ascii_nonnegative_decimal(cursor) -> None:
    async def exercise() -> None:
        async with run_runtime() as (_, coordinator, created, _, _, _):
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            with pytest.raises(InvalidEventCursorError):
                await coordinator.subscribe_session(
                    created.run_id,
                    session.session_id,
                    after_sequence=cursor,
                )

    run(exercise)


def test_session_cursor_replays_only_greater_ids_and_rejects_ahead() -> None:
    async def exercise() -> None:
        async with run_runtime() as (_, coordinator, created, _, _, _):
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            await coordinator.mark_agent_session_running(created.run_id, session.session_id)
            await coordinator.complete_agent_session(created.run_id, session.session_id)
            stream = await coordinator.subscribe_session(
                created.run_id,
                session.session_id,
                after_sequence="1",
            )
            assert [item.sequence async for item in stream] == [2]
            consumed = await coordinator.subscribe_session(
                created.run_id,
                session.session_id,
                after_sequence="2",
            )
            assert [item async for item in consumed] == []
            with pytest.raises(EventCursorAheadError):
                await coordinator.subscribe_session(
                    created.run_id,
                    session.session_id,
                    after_sequence="3",
                )
            with pytest.raises(EventCursorAheadError):
                await coordinator.subscribe_session(
                    created.run_id,
                    session.session_id,
                    after_sequence="9" * 5_000,
                )

    run(exercise)


def test_session_first_read_handoff_and_multiple_subscribers_have_no_gap() -> None:
    async def exercise() -> None:
        async with run_runtime(message_registry=True) as (
            _, coordinator, created, context, _, _,
        ):
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            first = await coordinator.subscribe_session(created.run_id, session.session_id)
            second = await coordinator.subscribe_session(created.run_id, session.session_id)
            message = await coordinator.start_transcript_message(
                created.run_id,
                session.session_id,
                "fixture_message",
                1,
            )
            assert [
                (await anext(first)).sequence,
                (await anext(first)).sequence,
            ] == [1, 2]
            assert [
                (await anext(second)).sequence,
                (await anext(second)).sequence,
            ] == [1, 2]
            await first.aclose()
            await second.aclose()
            assert not context.cancellation.is_requested()
            assert message.item_id in coordinator._runs[created.run_id].streaming_messages
            await coordinator.finalize_transcript_message(
                created.run_id,
                message.item_id,
                FixtureMessageV1(
                    text="",
                    generation_status=GenerationStatus.INCOMPLETE,
                ),
            )

    run(exercise)


def test_session_http_handshake_reports_cursor_and_identity_errors() -> None:
    async def exercise() -> None:
        async with run_runtime() as (app, coordinator, created, _, _, _):
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            path = (
                f"/api/analysis/runs/{created.run_id}/sessions/"
                f"{session.session_id}/events"
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                invalid = await client.get(path, headers={"Last-Event-ID": "-1"})
                assert invalid.status_code == 400
                assert invalid.json()["error"]["code"] == "invalid_event_cursor"
                ahead = await client.get(path, headers={"Last-Event-ID": "2"})
                assert ahead.status_code == 409
                assert ahead.json()["error"]["code"] == "event_cursor_ahead"
                missing = await client.get(
                    path.replace(str(session.session_id), str(uuid4()))
                )
                assert missing.status_code == 404
                assert missing.json()["error"]["code"] == "session_not_found"

    run(exercise)


def test_streaming_redaction_is_cross_chunk_and_final_body_is_stored_once() -> None:
    async def exercise() -> None:
        async with run_runtime(message_registry=True) as (
            _, coordinator, created, _, _, _,
        ):
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            message = await coordinator.start_transcript_message(
                created.run_id,
                session.session_id,
                "fixture_message",
                1,
            )
            await coordinator.append_transcript_message_delta(
                created.run_id, message.item_id, "Authorization: Be"
            )
            await coordinator.append_transcript_message_delta(
                created.run_id, message.item_id, "arer secret-token\nanswer"
            )
            await coordinator.finalize_transcript_message(
                created.run_id,
                message.item_id,
                FixtureMessageV1(
                    text="Authorization: Bearer secret-token\nanswer",
                    generation_status=GenerationStatus.INCOMPLETE,
                ),
            )
            runtime = coordinator._runs[created.run_id]
            records = runtime.agent_sessions[session.session_id].process_events
            deltas = "".join(
                item.text for item in records if isinstance(item, MessageDeltaProcessRecord)
            )
            assert deltas == "Authorization: [REDACTED]\nanswer"
            assert "secret-token" not in deltas
            assert sum(item.item_id == message.item_id for item in runtime.transcript_buffer.items) == 1
            finalized = next(
                item for item in records if isinstance(item, MessageFinalizedProcessRecord)
            )
            assert finalized.generation_status is GenerationStatus.INCOMPLETE

    run(exercise)


def test_two_streaming_messages_can_interleave_without_mixing_identity() -> None:
    async def exercise() -> None:
        async with run_runtime(message_registry=True) as (
            _, coordinator, created, _, _, _,
        ):
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            first = await coordinator.start_transcript_message(
                created.run_id, session.session_id, "fixture_message", 1
            )
            second = await coordinator.start_transcript_message(
                created.run_id, session.session_id, "fixture_message", 1
            )
            await coordinator.append_transcript_message_delta(
                created.run_id, first.item_id, "first"
            )
            await coordinator.append_transcript_message_delta(
                created.run_id, second.item_id, "second"
            )
            await coordinator.finalize_transcript_message(
                created.run_id,
                second.item_id,
                FixtureMessageV1(
                    text="second done",
                    generation_status=GenerationStatus.COMPLETED,
                ),
            )
            await coordinator.finalize_transcript_message(
                created.run_id,
                first.item_id,
                FixtureMessageV1(
                    text="first done",
                    generation_status=GenerationStatus.COMPLETED,
                ),
            )
            records = coordinator._runs[created.run_id].agent_sessions[
                session.session_id
            ].process_events
            message_records = records[1:]
            assert [type(item) for item in message_records] == [
                MessageStartedProcessRecord,
                MessageStartedProcessRecord,
                MessageDeltaProcessRecord,
                MessageDeltaProcessRecord,
                MessageFinalizedProcessRecord,
                MessageDeltaProcessRecord,
                MessageDeltaProcessRecord,
                MessageFinalizedProcessRecord,
            ]
            assert [
                item.item_id
                if isinstance(item, MessageDeltaProcessRecord)
                else item.item.item_id
                for item in message_records[2:]
            ] == [
                second.item_id,
                second.item_id,
                second.item_id,
                first.item_id,
                first.item_id,
                first.item_id,
            ]

    run(exercise)


def test_streamed_and_final_text_mismatch_records_visible_diagnostic() -> None:
    async def exercise() -> None:
        async with run_runtime(message_registry=True) as (
            _, coordinator, created, _, _, store,
        ):
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            message = await coordinator.start_transcript_message(
                created.run_id,
                session.session_id,
                "fixture_message",
                1,
            )
            await coordinator.append_transcript_message_delta(
                created.run_id,
                message.item_id,
                "streamed text",
            )
            await coordinator.finalize_transcript_message(
                created.run_id,
                message.item_id,
                FixtureMessageV1(
                    text="different final text",
                    generation_status=GenerationStatus.COMPLETED,
                ),
            )
            runtime = coordinator._runs[created.run_id]
            records = runtime.agent_sessions[session.session_id].process_events
            assert [type(item) for item in records[-2:]] == [
                MessageFinalizedProcessRecord,
                TranscriptItemProcessRecord,
            ]
            finalized = records[-2]
            assert isinstance(finalized, MessageFinalizedProcessRecord)
            assert finalized.generation_status is GenerationStatus.INCOMPLETE
            assert json.loads(finalized.item.content.safe_body) == {
                "text": "streamed text",
                "generation_status": "incomplete",
                "end_reason": "error",
            }
            assert records[-1].item.content.content_type == "error"
            assert sum(
                item.item_id == message.item_id
                for item in runtime.transcript_buffer.items
            ) == 1
            await coordinator._transcript_writer.drain(runtime)
            stored = await store.read_run(created.run_id)
            stored_message = next(
                item for item in stored.items if item.item_id == message.item_id
            )
            assert json.loads(stored_message.inline_body) == {
                "text": "streamed text",
                "generation_status": "incomplete",
                "end_reason": "error",
            }

    run(exercise)


def test_streamed_message_metadata_matches_started_finalized_get_and_storage() -> None:
    async def exercise() -> None:
        async with run_runtime(message_registry=True) as (
            app, coordinator, created, _, _, store,
        ):
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            await coordinator.mark_agent_session_running(
                created.run_id,
                session.session_id,
            )
            first_secret = "first-private-tag"
            second_secret = "second-private-tag"
            expected_tags = ["answer", "source:[REDACTED]", "water-quality"]
            message = await coordinator.start_transcript_message(
                created.run_id,
                session.session_id,
                "fixture_message",
                1,
                tags=(
                    "answer",
                    f"source:{first_secret}",
                    f"source:{second_secret}",
                    "water-quality",
                ),
                rag_eligible=True,
                private_values=(first_secret, second_secret),
            )
            await coordinator.append_transcript_message_delta(
                created.run_id,
                message.item_id,
                "metadata answer",
            )
            await coordinator.finalize_transcript_message(
                created.run_id,
                message.item_id,
                FixtureMessageV1(
                    text="metadata answer",
                    generation_status=GenerationStatus.COMPLETED,
                ),
            )
            await coordinator.complete_agent_session(
                created.run_id,
                session.session_id,
            )
            runtime = coordinator._runs[created.run_id]
            await coordinator._transcript_writer.drain(runtime)

            response = await create_session_event_response(
                coordinator,
                app.state.transcript_reader,
                created.run_id,
                session.session_id,
            )
            events = decoded_frames(await collect_response(response))
            started = next(
                data for name, _, data in events if name == "message_started"
            )
            finalized = next(
                data for name, _, data in events if name == "message_finalized"
            )
            for payload in (started, finalized):
                assert payload["payload"]["item"]["tags"] == expected_tags
                assert payload["payload"]["item"]["rag_eligible"] is True
            assert first_secret not in json.dumps(events)
            assert second_secret not in json.dumps(events)

            stored = await store.read_run(created.run_id)
            stored_message = next(
                item for item in stored.items if item.item_id == message.item_id
            )
            assert stored_message.tags == tuple(expected_tags)
            assert stored_message.rag_eligible is True

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                responses = [
                    await client.get(
                        f"/api/analysis/runs/{created.run_id}/sessions/"
                        f"{session.session_id}/transcript{suffix}"
                    )
                    for suffix in ("", "?view=history")
                ]
            for response in responses:
                assert response.status_code == 200
                assert first_secret not in response.text
                assert second_secret not in response.text
                response_message = next(
                    item for item in response.json()["items"]
                    if item["item_id"] == str(message.item_id)
                )
                assert response_message["tags"] == expected_tags
                assert response_message["rag_eligible"] is True

    run(exercise)


@pytest.mark.parametrize(
    ("expected", "chunks"),
    (
        ("https://example.com:8443/path", ("https://example.com:", "8443/", "path")),
        (
            "https://example.com?contact=alice@example.net",
            ("https://example.com?contact=", "alice@", "example.net"),
        ),
        (
            "https://example.com/path#contact=alice@example.net",
            ("https://example.com/path#contact=", "alice@", "example.net"),
        ),
        (
            "https://[2001:db8::1]:8443/path",
            ("https://[2001:db8::1]", ":8443/", "path"),
        ),
    ),
)
def test_streamed_normal_url_stays_completed_and_matches_final_body(
    expected: str,
    chunks: tuple[str, ...],
) -> None:
    async def exercise() -> None:
        async with run_runtime(message_registry=True) as (
            app, coordinator, created, _, _, store,
        ):
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            await coordinator.mark_agent_session_running(
                created.run_id,
                session.session_id,
            )
            message = await coordinator.start_transcript_message(
                created.run_id,
                session.session_id,
                "fixture_message",
                1,
            )
            for chunk in chunks:
                await coordinator.append_transcript_message_delta(
                    created.run_id,
                    message.item_id,
                    chunk,
                )
            await coordinator.finalize_transcript_message(
                created.run_id,
                message.item_id,
                FixtureMessageV1(
                    text=expected,
                    generation_status=GenerationStatus.COMPLETED,
                ),
            )
            await coordinator.complete_agent_session(
                created.run_id,
                session.session_id,
            )
            runtime = coordinator._runs[created.run_id]
            await coordinator._transcript_writer.drain(runtime)

            response = await create_session_event_response(
                coordinator,
                app.state.transcript_reader,
                created.run_id,
                session.session_id,
            )
            events = decoded_frames(await collect_response(response))
            streamed = "".join(
                data["payload"]["text"]
                for name, _, data in events
                if name == "message_delta"
                and data["payload"]["item_id"] == str(message.item_id)
            )
            finalized = next(
                data for name, _, data in events
                if name == "message_finalized"
                and data["payload"]["item"]["item_id"] == str(message.item_id)
            )
            assert streamed == expected
            assert finalized["payload"]["generation_status"] == "completed"
            assert finalized["payload"]["item"]["content"]["body"]["text"] == expected
            assert not any(
                item.content.content_type == "error"
                for item in runtime.transcript_buffer.items
                if item.item_id != message.item_id
            )

            stored = await store.read_run(created.run_id)
            stored_message = next(
                item for item in stored.items if item.item_id == message.item_id
            )
            assert json.loads(stored_message.inline_body)["text"] == expected

    run(exercise)


def test_complete_private_url_is_hidden_in_deltas_replay_and_storage(
    application_logs,
) -> None:
    async def exercise() -> None:
        async with run_runtime(message_registry=True) as (
            app, coordinator, created, _, _, store,
        ):
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            await coordinator.mark_agent_session_running(
                created.run_id,
                session.session_id,
            )
            private_url = "https://private.example/path?query=value#fragment"
            message = await coordinator.start_transcript_message(
                created.run_id,
                session.session_id,
                "fixture_message",
                1,
                private_values=(private_url,),
            )
            for chunk in ("https://pri", "vate.example/path?", "query=value#fragment"):
                await coordinator.append_transcript_message_delta(
                    created.run_id,
                    message.item_id,
                    chunk,
                )
            await coordinator.finalize_transcript_message(
                created.run_id,
                message.item_id,
                FixtureMessageV1(
                    text=private_url,
                    generation_status=GenerationStatus.COMPLETED,
                ),
            )
            await coordinator.complete_agent_session(
                created.run_id,
                session.session_id,
            )
            runtime = coordinator._runs[created.run_id]
            await coordinator._transcript_writer.drain(runtime)

            response = await create_session_event_response(
                coordinator,
                app.state.transcript_reader,
                created.run_id,
                session.session_id,
            )
            events = decoded_frames(await collect_response(response))
            serialized = json.dumps(events)
            streamed = "".join(
                data["payload"]["text"]
                for name, _, data in events
                if name == "message_delta"
                and data["payload"]["item_id"] == str(message.item_id)
            )
            assert streamed == "[REDACTED]"
            assert private_url not in serialized

            stored = await store.read_run(created.run_id)
            stored_message = next(
                item for item in stored.items if item.item_id == message.item_id
            )
            assert json.loads(stored_message.inline_body)["text"] == "[REDACTED]"
            assert private_url not in "\n".join(application_logs)

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                for suffix in ("", "?view=history"):
                    transcript = await client.get(
                        f"/api/analysis/runs/{created.run_id}/sessions/"
                        f"{session.session_id}/transcript{suffix}"
                    )
                    assert transcript.status_code == 200
                    assert private_url not in transcript.text
                    assert "[REDACTED]" in transcript.text

    run(exercise)


def test_terminal_session_returns_200_then_unnumbered_stream_end() -> None:
    async def exercise() -> None:
        async with run_runtime() as (app, coordinator, created, _, _, _):
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            await coordinator.mark_agent_session_running(created.run_id, session.session_id)
            await coordinator.complete_agent_session(created.run_id, session.session_id)
            response = await create_session_event_response(
                coordinator,
                app.state.transcript_reader,
                created.run_id,
                session.session_id,
                last_event_id="2",
            )
            messages = await collect_response(response)
            assert messages[0]["status"] == 200
            assert len(frames(messages)) == 1
            assert frames(messages)[0].startswith("event: stream_end\ndata: {")
            assert "id:" not in frames(messages)[0]

    run(exercise)


def test_session_response_encodes_all_phases_and_large_final_as_reference() -> None:
    async def exercise() -> None:
        async with run_runtime(
            message_registry=True,
            settings=AppSettings(
                transcript_inline_max_bytes=256,
                transcript_preview_bytes=64,
            ),
        ) as (
            app, coordinator, created, _, release, _,
        ):
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            await coordinator.mark_agent_session_running(created.run_id, session.session_id)
            message = await coordinator.start_transcript_message(
                created.run_id,
                session.session_id,
                "fixture_message",
                1,
            )
            text = "水" * 100
            await coordinator.append_transcript_message_delta(
                created.run_id,
                message.item_id,
                text,
            )
            await coordinator.finalize_transcript_message(
                created.run_id,
                message.item_id,
                FixtureMessageV1(
                    text=text,
                    generation_status=GenerationStatus.COMPLETED,
                ),
            )
            await coordinator.complete_agent_session(created.run_id, session.session_id)
            response = await create_session_event_response(
                coordinator,
                app.state.transcript_reader,
                created.run_id,
                session.session_id,
            )
            events = decoded_frames(await collect_response(response))
            names = [name for name, _, _ in events]
            assert names[:2] == ["transcript_item", "message_started"]
            assert names[2:-3] and set(names[2:-3]) == {"message_delta"}
            assert names[-3:] == ["message_finalized", "transcript_item", "stream_end"]
            assert [event_id for _, event_id, _ in events[:-1]] == list(
                range(1, len(events))
            )
            assert events[-1][1] is None
            started = events[1][2]
            deltas = [data for name, _, data in events if name == "message_delta"]
            finalized = next(data for name, _, data in events if name == "message_finalized")
            assert started["payload"]["item"]["item_id"] == str(message.item_id)
            assert "".join(delta["payload"]["text"] for delta in deltas) == text
            assert all(
                delta["payload"]["item_id"] == str(message.item_id) for delta in deltas
            )
            assert finalized["payload"]["item"]["item_id"] == str(message.item_id)
            assert finalized["payload"]["generation_status"] == "completed"
            assert finalized["payload"]["item"]["content"]["kind"] == "reference"
            assert finalized["payload"]["item"]["content_ref"] == {
                "run_id": str(created.run_id),
                "item_id": str(message.item_id),
            }
            assert started["occurred_at"] != deltas[0]["occurred_at"]
            assert deltas[-1]["occurred_at"] != finalized["occurred_at"]
            release.set()

    run(exercise)


def test_session_ends_without_waiting_for_run_and_run_ends_after_quiescence() -> None:
    async def exercise() -> None:
        async with run_runtime() as (app, coordinator, created, _, release, store):
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            await coordinator.mark_agent_session_running(created.run_id, session.session_id)
            await coordinator.complete_agent_session(created.run_id, session.session_id)
            session_response = await create_session_event_response(
                coordinator,
                app.state.transcript_reader,
                created.run_id,
                session.session_id,
                last_event_id="2",
            )
            assert len(frames(await collect_response(session_response))) == 1
            assert (await coordinator.get_snapshot(created.run_id)).status is RunStatus.RUNNING
            release.set()
            await coordinator._runs[created.run_id].workflow_quiesced.wait()
            run_response = await create_run_event_response(
                coordinator,
                app.state.transcript_reader,
                created.run_id,
            )
            sent = await collect_response(run_response)
            assert frames(sent)[0].startswith("event: run_snapshot\n")
            assert frames(sent)[-1].startswith("event: stream_end\n")
            assert all("id:" not in frame for frame in frames(sent))

    run(exercise)


def test_released_live_stream_returns_history_location_but_missing_is_404() -> None:
    async def exercise() -> None:
        async with run_runtime() as (app, coordinator, created, _, release, store):
            release.set()
            runtime = coordinator._runs[created.run_id]
            await runtime.workflow_quiesced.wait()
            await coordinator._transcript_writer.drain(runtime)
            coordinator._runs.pop(created.run_id)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                released = await client.get(
                    f"/api/analysis/runs/{created.run_id}/events"
                )
                assert released.status_code == 410
                assert released.json()["error"]["code"] == "live_state_gone"
                assert released.json()["history_url"].endswith(
                    f"/{created.run_id}?view=history"
                )
                original_read = store.read_run

                async def fail_read(run_id):
                    raise OSError("database unavailable")

                store.read_run = fail_read
                unavailable = await client.get(
                    f"/api/analysis/runs/{uuid4()}/events"
                )
                assert unavailable.status_code == 503
                assert unavailable.json()["error"]["code"] == "database_unavailable"
                store.read_run = original_read
            with pytest.raises(LiveStreamReleasedError) as error:
                await create_run_event_response(
                    coordinator,
                    app.state.transcript_reader,
                    created.run_id,
                )
            assert error.value.history_url.endswith(f"/{created.run_id}?view=history")
            with pytest.raises(RunNotFoundError):
                await create_run_event_response(
                    coordinator,
                    app.state.transcript_reader,
                    uuid4(),
                )

    run(exercise)


@pytest.mark.parametrize("interval", [0, -1, float("inf"), float("nan"), True])
def test_keepalive_interval_must_be_finite_and_positive(interval) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        RunCoordinator(
            TickingClock(),
            model_catalog=MODEL_CATALOG,
            transcript_store=InMemoryTranscriptStore(),
            workflow=NoOpWorkflow(),
            keepalive_interval=interval,
        )
