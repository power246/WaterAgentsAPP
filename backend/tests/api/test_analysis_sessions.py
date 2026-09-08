import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import ClientDisconnect

from app.api.schemas.analysis_sessions import AnalysisSessionErrorResponse, AnalysisSessionResponse
from app.application.analysis_sessions.models import SessionStatus
from app.application.analysis_sessions.workflow import WorkflowContext, WorkflowFailed
from app.application.correlation import request_id_context
from app.infrastructure.config import AppSettings
from app.main import create_app

BASE = "/api/analysis/sessions"
SECRET = "private-token-and-query-do-not-expose"
TIMESTAMP = "2026-09-08T03:20:00Z"


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 9, 8, 3, 20, tzinfo=timezone.utc)


class SequentialIds:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> UUID:
        self.count += 1
        return UUID(int=self.count)


class GatedWorkflow:
    def __init__(self) -> None:
        self.started: asyncio.Queue[tuple[WorkflowContext, asyncio.Event, UUID | None]] = asyncio.Queue()
        self.error: Exception | None = None

    async def run(self, context: WorkflowContext) -> None:
        release = asyncio.Event()
        self.started.put_nowait((context, release, request_id_context.get()))
        await release.wait()
        context.cancellation.checkpoint()
        if self.error:
            raise self.error


@asynccontextmanager
async def http_application(*, workflow=None, settings=None, **options):
    application = create_app(
        settings, clock=FixedClock(), workflow=workflow, shutdown_grace_period=0, **options
    )
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://testserver"
        ) as client:
            yield application, client
    assert all(
        runtime.workflow_task.done()
        for runtime in application.state.session_coordinator._sessions.values()
    )
    assert request_id_context.get() is None


def run(exercise) -> None:
    asyncio.run(asyncio.wait_for(exercise(), timeout=5))


def assert_error(response, status, code, *, session_id=None):
    assert response.status_code == status
    assert response.headers["content-type"] == "application/json"
    envelope = AnalysisSessionErrorResponse.model_validate(response.json())
    assert envelope.error.code == code
    assert str(envelope.error.request_id) == response.headers["x-request-id"]
    assert envelope.error.error_id != envelope.error.request_id
    assert envelope.error.session_id == (UUID(session_id) if session_id else None)
    assert set(response.json()) == {"error", "session", "validation"}
    assert SECRET not in response.text
    assert "Traceback" not in response.text
    assert "/Users/" not in response.text
    return envelope


def frames_from(messages):
    return [message["body"] for message in messages if message.get("body")]


def events_from(messages):
    return [
        json.loads(line[6:])
        for frame in frames_from(messages)
        for line in frame.decode("utf-8").split("\n")
        if line.startswith("data: ")
    ]


async def call_stream(application, path, *, send_hook=None, disconnect=None, spec="2.4", headers=()):
    messages = []
    sent_request = False

    async def receive():
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await (disconnect if disconnect is not None else asyncio.Event()).wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)
        if send_hook:
            await send_hook(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": spec},
        "http_version": "1.1", "method": "GET", "scheme": "http", "path": path,
        "raw_path": path.encode("utf-8"), "root_path": "", "query_string": b"",
        "headers": list(headers), "server": ("testserver", 80), "client": ("127.0.0.1", 1234),
    }
    await application(scope, receive, send)
    return messages


@pytest.mark.parametrize("resolution", [None, "6h", "12h", "24h"])
@pytest.mark.parametrize("zone", ["America/Chicago", "Asia/Shanghai"])
def test_create_exact_pending_snapshot_and_configuration(resolution, zone) -> None:
    async def exercise() -> None:
        workflow = GatedWorkflow()
        settings = AppSettings(analysis_timezone=zone)
        async with http_application(workflow=workflow, settings=settings, id_factory=SequentialIds()) as (app, client):
            body = {"query": "  private query  "}
            if resolution is not None:
                body["time_resolution"] = resolution
            response = await client.post(BASE, json=body)
            assert response.status_code == 201
            assert response.headers["x-request-id"] == str(UUID(int=1))
            expected = {
                "session_id": str(UUID(int=2)), "status": "pending",
                "created_at": TIMESTAMP, "updated_at": TIMESTAMP,
                "configuration": {
                    "analysis_timezone": zone, "time_resolution": resolution or "12h",
                    "time_resolution_source": "frontend" if resolution else "default",
                },
            }
            assert response.json() == expected
            AnalysisSessionResponse.model_validate(response.json())
            context, release, request_id = await workflow.started.get()
            assert request_id == UUID(response.headers["x-request-id"])
            assert context.query == "private query"
            assert context.session.status is SessionStatus.RUNNING
            assert app.state.settings is settings
            settings.analysis_timezone = "Europe/London"
            assert context.session.configuration.analysis_timezone == zone
            release.set()
            runtime = app.state.session_coordinator._sessions[context.session.session_id]
            await runtime.workflow_quiesced.wait()
            assert response.json() == expected
            replay = await client.get(f"{BASE}/{expected['session_id']}/events")
            assert replay.status_code == 200
            assert "private query" not in replay.text
            assert '"analysis_timezone":"' + zone + '"' in replay.text

    run(exercise)


def test_default_workflow_completes_and_idempotency_key_is_not_reused() -> None:
    async def exercise() -> None:
        async with http_application() as (app, client):
            responses = [await client.post(BASE, json={"query": "query"}, headers={"Idempotency-Key": "same"}) for _ in range(2)]
            ids = [response.json()["session_id"] for response in responses]
            assert len(set(ids)) == 2
            assert all(response.json()["status"] == "pending" for response in responses)
            for session_id in ids:
                replay = await client.get(f"{BASE}/{session_id}/events")
                assert replay.status_code == 200
                assert replay.headers["content-type"] == "text/event-stream; charset=utf-8"
                assert replay.headers["cache-control"] == "no-cache"
                assert "session_created" in replay.text and "session_completed" in replay.text
                assert "session_started" not in replay.text
                consumed = await client.get(f"{BASE}/{session_id}/events", headers={"Last-Event-ID": "2"})
                assert consumed.status_code == 204
                assert consumed.content == b""
                assert UUID(consumed.headers["x-request-id"])
            assert len(app.state.session_coordinator._sessions) == 2

    run(exercise)


@pytest.mark.parametrize("body", [
    {}, {"query": ""}, {"query": "  \n "}, {"query": None}, {"query": 123},
    {"query": [SECRET]}, {"query": {SECRET: SECRET}},
    {"query": SECRET, "time_resolution": None}, {"query": SECRET, "time_resolution": "auto"},
    {"query": SECRET, "time_resolution": SECRET}, {"query": SECRET, "fake_outcome": "failed"},
    {"query": SECRET, SECRET: SECRET}, [SECRET], None,
])
def test_validation_uses_safe_envelope_and_creates_no_session(body, caplog) -> None:
    caplog.set_level(logging.INFO)

    async def exercise() -> None:
        async with http_application() as (app, client):
            response = await client.post(BASE, content=json.dumps(body), headers={"Content-Type": "application/json"})
            envelope = assert_error(response, 422, "invalid_request")
            assert envelope.session is None
            assert envelope.validation
            assert envelope.error.failed_stage is None
            assert all(set(item) == {"field", "type", "message"} for item in response.json()["validation"])
            assert app.state.session_coordinator._sessions == {}
            assert SECRET not in repr([record.__dict__ for record in caplog.records])
            if isinstance(body, dict) and SECRET in body:
                assert ["body", "unknown_field"] in [item["field"] for item in response.json()["validation"]]

    run(exercise)


def test_malformed_json_and_wrong_content_type_have_safe_validation() -> None:
    async def exercise() -> None:
        async with http_application() as (_, client):
            malformed = await client.post(BASE, content='{"query":"' + SECRET, headers={"Content-Type": "application/json"})
            envelope = assert_error(malformed, 422, "invalid_request")
            assert envelope.validation[0].type == "json_invalid"
            assert envelope.validation[0].message == "The request body must contain valid JSON."
            wrong_type = await client.post(BASE, content=SECRET, headers={"Content-Type": "text/plain"})
            assert_error(wrong_type, 422, "invalid_request")

    run(exercise)


@pytest.mark.parametrize("running", [False, True])
def test_cancel_is_immediate_idempotent_and_replays_one_terminal(running, caplog) -> None:
    caplog.set_level(logging.INFO, logger="wateragents.analysis_sessions")

    async def exercise() -> None:
        workflow = GatedWorkflow()
        async with http_application(workflow=workflow) as (app, client):
            created = await client.post(BASE, json={"query": SECRET})
            session_id = created.json()["session_id"]
            runtime = app.state.session_coordinator._sessions[UUID(session_id)]
            release = None
            if running:
                _, release, _ = await workflow.started.get()
            else:
                assert runtime.snapshot.status is SessionStatus.PENDING
            response = await client.post(f"{BASE}/{session_id}/cancel")
            assert response.status_code == 200
            assert response.json() == {**created.json(), "status": "cancelled"}
            assert not runtime.workflow_quiesced.is_set()
            assert runtime.workflow_task.cancelling() == 0
            repeated = await client.post(f"{BASE}/{session_id}/cancel")
            assert repeated.json() == response.json()
            assert repeated.headers["x-request-id"] != response.headers["x-request-id"]
            if release:
                release.set()
            await runtime.workflow_quiesced.wait()
            stream = await client.get(f"{BASE}/{session_id}/events")
            assert "session_cancelled" in stream.text
            assert "session_completed" not in stream.text
            assert len(runtime.events) == 2
            logs = [record for record in caplog.records if getattr(record, "event_name", None) == "cancel_accepted"]
            assert len(logs) == 1
            assert str(logs[0].correlation.request_id) == response.headers["x-request-id"]

    run(exercise)


@pytest.mark.parametrize("failure", [False, True])
def test_terminal_cancel_conflict_returns_current_snapshot_and_new_error_id(failure) -> None:
    async def exercise() -> None:
        workflow = GatedWorkflow()
        async with http_application(workflow=workflow) as (app, client):
            created = await client.post(BASE, json={"query": SECRET})
            session_id = created.json()["session_id"]
            _, release, _ = await workflow.started.get()
            if failure:
                workflow.error = WorkflowFailed(SECRET)
            release.set()
            runtime = app.state.session_coordinator._sessions[UUID(session_id)]
            await runtime.workflow_quiesced.wait()
            response = await client.post(f"{BASE}/{session_id}/cancel")
            envelope = assert_error(response, 409, "session_already_terminal", session_id=session_id)
            assert envelope.session.model_dump() == runtime.snapshot.model_dump()
            assert envelope.error.message == f"The session is already {'failed' if failure else 'completed'}."
            assert envelope.error.failed_stage == "cancel_session"
            assert envelope.error.retryable is False
            if failure:
                assert envelope.error.error_id != runtime.last_error.error_id
            assert len(runtime.events) == 2

    run(exercise)


@pytest.mark.parametrize("endpoint,method", [("events", "GET"), ("cancel", "POST")])
def test_session_lookup_errors_have_uniform_uuid_and_not_found_responses(endpoint, method) -> None:
    async def exercise() -> None:
        async with http_application() as (_, client):
            missing = str(uuid4())
            unknown = await client.request(method, f"{BASE}/{missing}/{endpoint}")
            assert_error(unknown, 404, "session_not_found", session_id=missing)
            invalid = await client.request(method, f"{BASE}/{SECRET}/{endpoint}")
            envelope = assert_error(invalid, 422, "invalid_request")
            assert envelope.validation[0].field == ("path", "session_id")
            assert envelope.validation[0].type == "uuid_parsing"

    run(exercise)


@pytest.mark.parametrize("cursor,status,code", [("", 400, "invalid_event_cursor"), ("-1", 400, "invalid_event_cursor"), (SECRET, 400, "invalid_event_cursor"), ("3", 409, "event_cursor_ahead")])
def test_cursor_errors_are_json_before_stream_start(cursor, status, code) -> None:
    async def exercise() -> None:
        async with http_application() as (_, client):
            created = await client.post(BASE, json={"query": "query"})
            session_id = created.json()["session_id"]
            response = await client.get(f"{BASE}/{session_id}/events", headers={"Last-Event-ID": cursor})
            envelope = assert_error(response, status, code, session_id=session_id)
            assert envelope.session is None
            assert envelope.validation == ()
            assert envelope.error.failed_stage == "subscribe_events"

    run(exercise)


@pytest.mark.parametrize("method,path,status,code", [
    ("GET", "/missing", 404, "not_found"), ("GET", "/health/", 404, "not_found"),
    ("POST", BASE + "/", 404, "not_found"), ("GET", BASE, 405, "method_not_allowed"),
    ("PUT", "/health", 405, "method_not_allowed"),
    ("GET", BASE + "/" + str(UUID(int=99)), 404, "not_found"),
])
def test_unknown_routes_and_methods_use_the_same_envelope(method, path, status, code) -> None:
    async def exercise() -> None:
        async with http_application() as (_, client):
            response = await client.request(method, path)
            envelope = assert_error(response, status, code)
            assert envelope.session is None and envelope.validation == ()
            if status == 405:
                assert response.headers["allow"]
            assert "location" not in response.headers

    run(exercise)


def test_unexpected_failure_is_safe_and_correlates_to_logs(monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO)

    async def exercise() -> None:
        async with http_application() as (app, client):
            async def fail(*args, **kwargs):
                raise RuntimeError(SECRET + " /Users/private/source.py")

            monkeypatch.setattr(app.state.session_coordinator, "create_session", fail)
            response = await client.post(BASE, json={"query": SECRET}, headers={"Authorization": SECRET, "Cookie": SECRET})
            envelope = assert_error(response, 500, "internal_error")
            assert envelope.session is None and envelope.validation == ()
            records = [record for record in caplog.records if getattr(record, "event_name", None) == "http_request_failed"]
            assert len(records) == 1
            assert records[0].public_error.model_dump() == envelope.error.model_dump()
            assert records[0].correlation.request_id == envelope.error.request_id
            assert records[0].correlation.error_id == envelope.error.error_id
            assert records[0].exception_type == "RuntimeError"
            assert SECRET not in repr([record.__dict__ for record in caplog.records])
            completed = [record for record in caplog.records if getattr(record, "event_name", None) == "http_request_completed"]
            assert completed[-1].status_code == 500
            assert completed[-1].route == BASE

    run(exercise)


def test_http_exception_detail_is_not_public_and_allow_is_preserved(monkeypatch) -> None:
    async def exercise() -> None:
        async with http_application() as (app, client):
            async def fail(*args, **kwargs):
                raise HTTPException(405, detail={"secret": SECRET}, headers={"Allow": "POST"})

            monkeypatch.setattr(app.state.session_coordinator, "create_session", fail)
            response = await client.post(BASE, json={"query": SECRET})
            assert_error(response, 405, "method_not_allowed")
            assert response.headers["allow"] == "POST"

    run(exercise)


def test_closing_coordinator_returns_retryable_service_unavailable() -> None:
    async def exercise() -> None:
        async with http_application() as (app, client):
            await app.state.session_coordinator.shutdown()
            response = await client.post(BASE, json={"query": "query"})
            envelope = assert_error(response, 503, "service_unavailable")
            assert envelope.error.retryable is True
            assert app.state.session_coordinator._sessions == {}

    run(exercise)


def test_request_ids_are_server_owned_isolated_and_inherited_by_workflows(caplog) -> None:
    caplog.set_level(logging.INFO)

    async def exercise() -> None:
        workflow = GatedWorkflow()
        async with http_application(workflow=workflow) as (_, client):
            spoofed = str(uuid4())
            responses = await asyncio.gather(*(
                client.post(BASE, json={"query": f"query {index}"}, headers={"X-Request-ID": spoofed})
                for index in range(2)
            ))
            contexts = [await workflow.started.get() for _ in range(2)]
            by_session = {UUID(response.json()["session_id"]): UUID(response.headers["x-request-id"]) for response in responses}
            assert len(set(by_session.values())) == 2
            assert UUID(spoofed) not in by_session.values()
            assert request_id_context.get() is None
            for context, release, request_id in contexts:
                assert request_id == by_session[context.session.session_id]
                release.set()
                stream = await client.get(f"{BASE}/{context.session.session_id}/events")
                assert UUID(stream.headers["x-request-id"]) not in by_session.values()
            lifecycle = [record for record in caplog.records if getattr(record, "event_name", None) == "lifecycle_event_committed"]
            assert len(lifecycle) == 4
            for record in lifecycle:
                assert record.correlation.request_id == by_session[record.correlation.session_id]
            health = await client.get("/health", headers={"X-Request-ID": SECRET})
            assert health.json() == {"status": "healthy"}
            assert health.headers["x-request-id"] != SECRET
            assert UUID(health.headers["x-request-id"])
            schema = await client.get("/openapi.json")
            assert UUID(schema.headers["x-request-id"])
            assert SECRET not in repr([record.__dict__ for record in caplog.records])

    run(exercise)


def test_started_stream_receives_workflow_failure_with_correlated_error(caplog) -> None:
    caplog.set_level(logging.INFO)

    async def exercise() -> None:
        workflow = GatedWorkflow()
        async with http_application(workflow=workflow) as (app, client):
            created = await client.post(BASE, json={"query": SECRET})
            session_id = created.json()["session_id"]
            _, release, _ = await workflow.started.get()
            workflow.error = RuntimeError(SECRET)
            first_event = asyncio.Event()

            async def observe(message):
                if b"session_created" in message.get("body", b""):
                    first_event.set()

            task = asyncio.create_task(call_stream(app, f"{BASE}/{session_id}/events", send_hook=observe))
            try:
                await first_event.wait()
                assert not task.done()
                release.set()
                messages = await task
                assert messages[0]["status"] == 200
                headers = dict(messages[0]["headers"])
                assert UUID(headers[b"x-request-id"].decode())
                assert headers[b"x-request-id"].decode() != created.headers["x-request-id"]
                events = events_from(messages)
                assert [event["type"] for event in events] == ["session_created", "session_failed"]
                error = events[-1]["payload"]["error"]
                records = [record for record in caplog.records if getattr(record, "event_name", None) == "session_failed"]
                assert len(records) == 1
                assert str(records[0].correlation.error_id) == error["error_id"]
                assert str(records[0].correlation.request_id) == created.headers["x-request-id"]
                assert SECRET not in repr(messages)
                assert SECRET not in repr([record.__dict__ for record in caplog.records])
                assert messages[-1]["more_body"] is False
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    run(exercise)


@pytest.mark.parametrize("disconnect_mode", ["disconnect", "write_failure"])
def test_stream_disconnect_or_write_failure_does_not_cancel_workflow(disconnect_mode) -> None:
    async def exercise() -> None:
        workflow = GatedWorkflow()
        async with http_application(workflow=workflow) as (app, client):
            created = await client.post(BASE, json={"query": SECRET})
            session_id = created.json()["session_id"]
            context, release, _ = await workflow.started.get()
            first_event = asyncio.Event()
            disconnect = asyncio.Event()

            async def observe(message):
                if b"session_created" in message.get("body", b""):
                    first_event.set()
                    if disconnect_mode == "write_failure":
                        raise OSError("connection closed")

            task = asyncio.create_task(call_stream(
                app, f"{BASE}/{session_id}/events", send_hook=observe, disconnect=disconnect,
                spec="2.3" if disconnect_mode == "disconnect" else "2.4",
            ))
            try:
                await first_event.wait()
                if disconnect_mode == "disconnect":
                    disconnect.set()
                    await task
                else:
                    with pytest.raises(ClientDisconnect):
                        await task
                runtime = app.state.session_coordinator._sessions[UUID(session_id)]
                assert runtime.snapshot.status is SessionStatus.RUNNING
                assert not context.cancellation.is_requested()
                assert runtime.workflow_task.cancelling() == 0
                release.set()
                replay = await client.get(f"{BASE}/{session_id}/events", headers={"Last-Event-ID": "1"})
                assert "session_completed" in replay.text
                assert "session_created" not in replay.text
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    run(exercise)


def test_openapi_describes_only_the_supported_routes_and_actual_envelopes() -> None:
    schema = create_app().openapi()
    paths = schema["paths"]
    assert {path: set(operations) for path, operations in paths.items()} == {
        "/health": {"get"}, BASE: {"post"}, BASE + "/{session_id}/events": {"get"},
        BASE + "/{session_id}/cancel": {"post"},
    }
    create = paths[BASE]["post"]
    assert create["responses"]["201"]["content"]["application/json"]["schema"]["$ref"].endswith("/AnalysisSessionResponse")
    assert create["requestBody"]["required"] is True
    assert create["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/CreateAnalysisSessionRequest")
    cancel = paths[BASE + "/{session_id}/cancel"]["post"]
    assert "requestBody" not in cancel
    events = paths[BASE + "/{session_id}/events"]["get"]
    assert set(events["responses"]["200"]["content"]) == {"text/event-stream"}
    assert "content" not in events["responses"]["204"]
    assert any(parameter["in"] == "header" and parameter["name"] == "Last-Event-ID" for parameter in events["parameters"])
    assert set(schema["components"]["schemas"]["CreateAnalysisSessionRequest"]["properties"]) == {"query", "time_resolution"}
    assert "HTTPValidationError" not in schema["components"]["schemas"]
    for path in paths.values():
        for operation in path.values():
            for status, response in operation["responses"].items():
                assert response["headers"]["X-Request-ID"]["schema"] == {"type": "string", "format": "uuid"}
                if int(status) >= 400:
                    assert response["content"]["application/json"]["schema"]["$ref"].endswith("/AnalysisSessionErrorResponse")


def test_stream_keepalive_does_not_block_cancel_or_mix_request_contexts(caplog) -> None:
    caplog.set_level(logging.INFO)

    async def exercise() -> None:
        workflow = GatedWorkflow()
        async with http_application(workflow=workflow, keepalive_interval=0.000001) as (app, client):
            created = await client.post(BASE, json={"query": "query"})
            session_id = created.json()["session_id"]
            _, release, _ = await workflow.started.get()
            comment_received = asyncio.Event()

            async def observe(message):
                if message.get("body") == b": keepalive\n\n":
                    comment_received.set()

            task = asyncio.create_task(call_stream(
                app, f"{BASE}/{session_id}/events", send_hook=observe,
                headers=[(b"last-event-id", b"1")],
            ))
            try:
                await comment_received.wait()
                cancelled = await client.post(f"{BASE}/{session_id}/cancel")
                assert cancelled.status_code == 200
                messages = await task
                release.set()
                header_id = dict(messages[0]["headers"])[b"x-request-id"].decode()
                assert len({header_id, cancelled.headers["x-request-id"], created.headers["x-request-id"]}) == 3
                assert [event["type"] for event in events_from(messages)] == ["session_cancelled"]
                assert b": keepalive\n\n" in frames_from(messages)
                completed = [record for record in caplog.records if getattr(record, "event_name", None) == "http_request_completed"]
                assert len(completed) == 3
                accepted = [record for record in caplog.records if getattr(record, "event_name", None) == "cancel_accepted"]
                assert str(accepted[0].correlation.request_id) == cancelled.headers["x-request-id"]
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    run(exercise)


def test_failure_after_stream_headers_never_appends_http_error_json(monkeypatch) -> None:
    from app.api.sse import response as response_module

    async def exercise() -> None:
        workflow = GatedWorkflow()
        async with http_application(workflow=workflow) as (app, client):
            created = await client.post(BASE, json={"query": "query"})
            session_id = created.json()["session_id"]
            _, release, _ = await workflow.started.get()
            original_encode = response_module.encode_event

            def fail_terminal(event):
                if event.sequence == 2:
                    raise RuntimeError(SECRET)
                return original_encode(event)

            monkeypatch.setattr(response_module, "encode_event", fail_terminal)
            first_event = asyncio.Event()
            delivered = []

            async def observe(message):
                delivered.append(message)
                if message.get("body"):
                    first_event.set()

            task = asyncio.create_task(call_stream(app, f"{BASE}/{session_id}/events", send_hook=observe))
            try:
                await first_event.wait()
                release.set()
                with pytest.raises(RuntimeError, match=SECRET):
                    await task
                assert [message["status"] for message in delivered if message["type"] == "http.response.start"] == [200]
                assert [event["type"] for event in events_from(delivered)] == ["session_created"]
                assert SECRET not in repr(delivered)
                runtime = app.state.session_coordinator._sessions[UUID(session_id)]
                assert runtime.snapshot.status is SessionStatus.COMPLETED
                assert len(runtime.events) == 2
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    run(exercise)


def test_http_logging_failure_does_not_change_responses_or_leak_context() -> None:
    async def exercise() -> None:
        logger = logging.getLogger("wateragents.http")
        original_level = logger.level

        class BrokenHandler(logging.Handler):
            def emit(self, record):
                raise RuntimeError("logger unavailable")

        handler = BrokenHandler()
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            async with http_application() as (_, client):
                created = await client.post(BASE, json={"query": "query"})
                assert created.status_code == 201
                response = await client.get("/missing")
                assert_error(response, 404, "not_found")
                assert request_id_context.get() is None
        finally:
            logger.removeHandler(handler)
            logger.setLevel(original_level)

    run(exercise)
