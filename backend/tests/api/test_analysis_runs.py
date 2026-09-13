import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException

from app.api.schemas.analysis_runs import AnalysisRunErrorResponse, AnalysisRunResponse
from app.application.analysis_runs.models import RunStatus
from app.application.analysis_runs.workflow import WorkflowContext, WorkflowFailed
from app.application.correlation import request_id_context
from app.infrastructure.config import AppSettings
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from app.main import create_app

BASE = "/api/analysis/runs"
SECRET = "private-token-and-query-do-not-expose"
TIMESTAMP = "2026-09-08T03:20:00Z"
SUPPORTED_ERRORS = {
    (400, "invalid_event_cursor"),
    (400, "invalid_request"),
    (404, "not_found"),
    (404, "run_not_found"),
    (405, "method_not_allowed"),
    (409, "event_cursor_ahead"),
    (409, "idempotency_key_conflict"),
    (409, "run_already_terminal"),
    (422, "invalid_request"),
    (500, "internal_error"),
    (503, "service_unavailable"),
    (503, "creation_commit_unconfirmed"),
    (503, "database_unavailable"),
    (503, "idempotency_wait_timeout"),
}


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
    options.setdefault("transcript_store", InMemoryTranscriptStore())
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
        for runtime in application.state.run_coordinator._runs.values()
    )
    assert request_id_context.get() is None


def run(exercise) -> None:
    asyncio.run(asyncio.wait_for(exercise(), timeout=5))


def assert_error(response, status, code, *, run_id=None):
    assert (status, code) in SUPPORTED_ERRORS
    assert response.status_code == status
    assert response.headers["content-type"] == "application/json"
    envelope = AnalysisRunErrorResponse.model_validate(response.json())
    assert envelope.error.code == code
    assert str(envelope.error.request_id) == response.headers["x-request-id"]
    assert envelope.error.error_id != envelope.error.request_id
    assert envelope.error.run_id == (UUID(run_id) if run_id else None)
    assert set(response.json()) == {"error", "run", "validation"}
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
                "run_id": str(UUID(int=2)), "status": "pending",
                "created_at": TIMESTAMP, "updated_at": TIMESTAMP,
                "configuration": {
                    "analysis_timezone": zone, "time_resolution": resolution or "12h",
                    "time_resolution_source": "frontend" if resolution else "default",
                },
            }
            assert response.json() == expected
            AnalysisRunResponse.model_validate(response.json())
            context, release, request_id = await workflow.started.get()
            assert request_id == UUID(response.headers["x-request-id"])
            assert context.query == "private query"
            assert context.run.status is RunStatus.RUNNING
            assert app.state.settings is settings
            settings.analysis_timezone = "Europe/London"
            assert context.run.configuration.analysis_timezone == zone
            release.set()
            runtime = app.state.run_coordinator._runs[context.run.run_id]
            await runtime.workflow_quiesced.wait()
            assert response.json() == expected
            replay = await client.get(f"{BASE}/{expected['run_id']}/events")
            assert replay.status_code == 200
            assert "private query" not in replay.text
            assert '"analysis_timezone":"' + zone + '"' in replay.text

    run(exercise)


def test_application_forwards_the_record_identity_factory() -> None:
    async def exercise() -> None:
        workflow = GatedWorkflow()
        record_ids = SequentialIds()
        async with http_application(
            workflow=workflow,
            record_id_factory=record_ids,
        ) as (app, client):
            response = await client.post(BASE, json={"query": "query"})
            run_id = UUID(response.json()["run_id"])
            _, release, _ = await workflow.started.get()
            runtime = app.state.run_coordinator._runs[run_id]

            assert [item.item_id for item in runtime.transcript_items] == [
                UUID(int=1),
                UUID(int=2),
            ]
            assert [mutation.mutation_id for mutation in runtime.state_mutations] == [
                UUID(int=3),
                UUID(int=4),
            ]

            release.set()
            await runtime.workflow_quiesced.wait()
            assert runtime.state_mutations[-1].mutation_id == UUID(int=5)
            assert runtime.transcript_items[-1].item_id == UUID(int=6)

    run(exercise)


def test_default_workflow_completes_and_requests_without_a_key_are_independent() -> None:
    async def exercise() -> None:
        async with http_application() as (app, client):
            responses = [await client.post(BASE, json={"query": "query"}) for _ in range(2)]
            ids = [response.json()["run_id"] for response in responses]
            assert len(set(ids)) == 2
            assert all(response.json()["status"] == "pending" for response in responses)
            for run_id in ids:
                replay = await client.get(f"{BASE}/{run_id}/events")
                assert replay.status_code == 200
                assert replay.headers["content-type"] == "text/event-stream; charset=utf-8"
                assert replay.headers["cache-control"] == "no-cache"
                assert "run_created" in replay.text and "run_completed" in replay.text
                assert "session_started" not in replay.text
                consumed = await client.get(f"{BASE}/{run_id}/events", headers={"Last-Event-ID": "2"})
                assert consumed.status_code == 204
                assert consumed.content == b""
                assert UUID(consumed.headers["x-request-id"])
            assert len(app.state.run_coordinator._runs) == 2

    run(exercise)


def test_same_key_and_exact_body_return_original_receipt_with_new_request_id() -> None:
    async def exercise() -> None:
        workflow = GatedWorkflow()
        key = str(uuid4())
        raw_body = b'{"query":"lake","time_resolution":"6h"}'
        headers = {"Content-Type": "application/json", "Idempotency-Key": key}
        async with http_application(workflow=workflow) as (app, client):
            first = await client.post(BASE, content=raw_body, headers=headers)
            second = await client.post(BASE, content=raw_body, headers=headers)
            assert first.status_code == second.status_code == 201
            assert first.json() == second.json()
            assert first.headers["x-request-id"] != second.headers["x-request-id"]
            _, release, _ = await workflow.started.get()
            assert workflow.started.empty()
            assert len(app.state.run_coordinator._runs) == 1
            release.set()

    run(exercise)


@pytest.mark.parametrize(
    "changed_body",
    [
        b'{ "query":"lake","time_resolution":"6h"}',
        b'{"time_resolution":"6h","query":"lake"}',
        b'{"query":"l\\u0061ke","time_resolution":"6h"}',
        b'{"query":"river","time_resolution":"6h"}',
        b'{"query":"lake"}',
    ],
)
def test_same_key_rejects_every_raw_body_change(changed_body: bytes) -> None:
    async def exercise() -> None:
        key = str(uuid4())
        headers = {"Content-Type": "application/json", "Idempotency-Key": key}
        async with http_application() as (app, client):
            first = await client.post(
                BASE,
                content=b'{"query":"lake","time_resolution":"6h"}',
                headers=headers,
            )
            assert first.status_code == 201
            conflict = await client.post(BASE, content=changed_body, headers=headers)
            envelope = assert_error(conflict, 409, "idempotency_key_conflict")
            assert envelope.error.retryable is False
            assert envelope.error.failed_stage == "create_run"
            assert len(app.state.run_coordinator._runs) == 1

    run(exercise)


@pytest.mark.parametrize(
    "key",
    ["not-a-uuid", str(UUID(int=0)), "6ba7b810-9dad-11d1-80b4-00c04fd430c8"],
)
def test_idempotency_key_must_be_uuid_v4(key: str) -> None:
    async def exercise() -> None:
        async with http_application() as (app, client):
            response = await client.post(
                BASE,
                json={"query": "lake"},
                headers={"Idempotency-Key": key},
            )
            envelope = assert_error(response, 422, "invalid_request")
            assert envelope.validation[0].field == ("header", "idempotency-key")
            assert app.state.run_coordinator._runs == {}

    run(exercise)


def test_different_keys_and_missing_keys_do_not_deduplicate_equal_bodies() -> None:
    async def exercise() -> None:
        raw_body = b'{"query":"lake"}'
        async with http_application() as (app, client):
            requests = [
                await client.post(
                    BASE,
                    content=raw_body,
                    headers={
                        "Content-Type": "application/json",
                        "Idempotency-Key": str(uuid4()),
                    },
                ),
                await client.post(
                    BASE,
                    content=raw_body,
                    headers={
                        "Content-Type": "application/json",
                        "Idempotency-Key": str(uuid4()),
                    },
                ),
                await client.post(
                    BASE,
                    content=raw_body,
                    headers={"Content-Type": "application/json"},
                ),
            ]
            assert all(response.status_code == 201 for response in requests)
            assert len({response.json()["run_id"] for response in requests}) == 3
            assert len(app.state.run_coordinator._runs) == 3

    run(exercise)


def test_receipt_survives_terminal_state_live_removal_restart_and_new_defaults() -> None:
    async def exercise() -> None:
        store = InMemoryTranscriptStore()
        workflow = GatedWorkflow()
        key = str(uuid4())
        raw_body = b'{"query":"lake"}'
        headers = {"Content-Type": "application/json", "Idempotency-Key": key}
        first_settings = AppSettings(analysis_timezone="America/Chicago")
        async with http_application(
            workflow=workflow,
            settings=first_settings,
            transcript_store=store,
        ) as (app, client):
            first = await client.post(BASE, content=raw_body, headers=headers)
            _, release, _ = await workflow.started.get()
            release.set()
            runtime = app.state.run_coordinator._runs[UUID(first.json()["run_id"])]
            await runtime.workflow_quiesced.wait()
            app.state.run_coordinator._runs.pop(runtime.snapshot.run_id)
            after_cleanup = await client.post(BASE, content=raw_body, headers=headers)
            assert after_cleanup.json() == first.json()
            assert after_cleanup.headers["x-request-id"] != first.headers["x-request-id"]

        second_workflow = GatedWorkflow()
        second_settings = AppSettings(analysis_timezone="Asia/Shanghai")
        async with http_application(
            workflow=second_workflow,
            settings=second_settings,
            transcript_store=store,
        ) as (app, client):
            replay = await client.post(BASE, content=raw_body, headers=headers)
            assert replay.status_code == 201
            assert replay.json() == first.json()
            assert replay.json()["configuration"]["analysis_timezone"] == "America/Chicago"
            assert app.state.run_coordinator._runs == {}
            assert second_workflow.started.empty()

    run(exercise)


@pytest.mark.parametrize("body", [
    {}, {"query": ""}, {"query": "  \n "}, {"query": None}, {"query": 123},
    {"query": [SECRET]}, {"query": {SECRET: SECRET}},
    {"query": SECRET, "time_resolution": None}, {"query": SECRET, "time_resolution": "auto"},
    {"query": SECRET, "time_resolution": SECRET}, {"query": SECRET, "fake_outcome": "failed"},
    {"query": SECRET, SECRET: SECRET}, [SECRET], None,
])
def test_validation_uses_safe_envelope_and_creates_no_run(body, caplog, application_logs) -> None:
    caplog.set_level(logging.INFO)

    async def exercise() -> None:
        async with http_application() as (app, client):
            response = await client.post(BASE, content=json.dumps(body), headers={"Content-Type": "application/json"})
            envelope = assert_error(response, 422, "invalid_request")
            assert envelope.run is None
            assert envelope.validation
            assert envelope.error.failed_stage is None
            assert all(set(item) == {"field", "type", "message"} for item in response.json()["validation"])
            assert app.state.run_coordinator._runs == {}
            assert SECRET not in "".join(application_logs)
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
            invalid_encoding = await client.post(BASE, content=b"\xff", headers={"Content-Type": "application/json"})
            envelope = assert_error(invalid_encoding, 400, "invalid_request")
            assert envelope.validation == ()

    run(exercise)


@pytest.mark.parametrize("running", [False, True])
def test_cancel_is_immediate_idempotent_and_replays_one_terminal(running, caplog, application_logs) -> None:
    caplog.set_level(logging.INFO, logger="wateragents.analysis_runs")

    async def exercise() -> None:
        workflow = GatedWorkflow()
        async with http_application(workflow=workflow) as (app, client):
            created = await client.post(BASE, json={"query": SECRET})
            run_id = created.json()["run_id"]
            runtime = app.state.run_coordinator._runs[UUID(run_id)]
            release = None
            if running:
                _, release, _ = await workflow.started.get()
            else:
                assert runtime.snapshot.status is RunStatus.PENDING
            response = await client.post(f"{BASE}/{run_id}/cancel")
            assert response.status_code == 200
            assert response.json() == {**created.json(), "status": "cancelled"}
            assert not runtime.workflow_quiesced.is_set()
            assert runtime.workflow_task.cancelling() == 0
            repeated = await client.post(f"{BASE}/{run_id}/cancel")
            assert repeated.json() == response.json()
            assert repeated.headers["x-request-id"] != response.headers["x-request-id"]
            if release:
                release.set()
            await runtime.workflow_quiesced.wait()
            stream = await client.get(f"{BASE}/{run_id}/events")
            assert "run_cancelled" in stream.text
            assert "run_completed" not in stream.text
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
            run_id = created.json()["run_id"]
            _, release, _ = await workflow.started.get()
            if failure:
                workflow.error = WorkflowFailed(SECRET)
            release.set()
            runtime = app.state.run_coordinator._runs[UUID(run_id)]
            await runtime.workflow_quiesced.wait()
            response = await client.post(f"{BASE}/{run_id}/cancel")
            envelope = assert_error(response, 409, "run_already_terminal", run_id=run_id)
            assert envelope.run.model_dump() == runtime.snapshot.model_dump()
            assert envelope.error.message == f"The run is already {'failed' if failure else 'completed'}."
            assert envelope.error.failed_stage == "cancel_run"
            assert envelope.error.retryable is False
            if failure:
                assert envelope.error.error_id != runtime.last_error.error_id
            assert len(runtime.events) == 2

    run(exercise)


@pytest.mark.parametrize("endpoint,method", [("events", "GET"), ("cancel", "POST")])
def test_run_lookup_errors_have_uniform_uuid_and_not_found_responses(endpoint, method) -> None:
    async def exercise() -> None:
        async with http_application() as (_, client):
            missing = str(uuid4())
            unknown = await client.request(method, f"{BASE}/{missing}/{endpoint}")
            assert_error(unknown, 404, "run_not_found", run_id=missing)
            invalid = await client.request(method, f"{BASE}/{SECRET}/{endpoint}")
            envelope = assert_error(invalid, 422, "invalid_request")
            assert envelope.validation[0].field == ("path", "run_id")
            assert envelope.validation[0].type == "uuid_parsing"

    run(exercise)


@pytest.mark.parametrize("cursor,status,code", [("", 400, "invalid_event_cursor"), ("-1", 400, "invalid_event_cursor"), (SECRET, 400, "invalid_event_cursor"), ("3", 409, "event_cursor_ahead")])
def test_cursor_errors_are_json_before_stream_start(cursor, status, code) -> None:
    async def exercise() -> None:
        async with http_application() as (_, client):
            created = await client.post(BASE, json={"query": "query"})
            run_id = created.json()["run_id"]
            response = await client.get(f"{BASE}/{run_id}/events", headers={"Last-Event-ID": cursor})
            envelope = assert_error(response, status, code, run_id=run_id)
            assert envelope.run is None
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
            assert envelope.run is None and envelope.validation == ()
            if status == 405:
                assert response.headers["allow"]
            assert "location" not in response.headers

    run(exercise)


def test_unexpected_failure_is_safe_and_correlates_to_logs(monkeypatch, caplog, application_logs) -> None:
    caplog.set_level(logging.INFO)

    async def exercise() -> None:
        async with http_application() as (app, client):
            async def fail(*args, **kwargs):
                raise RuntimeError(SECRET + " /Users/private/source.py")

            monkeypatch.setattr(app.state.run_coordinator, "create_run", fail)
            response = await client.post(BASE, json={"query": SECRET}, headers={"Authorization": SECRET, "Cookie": SECRET})
            envelope = assert_error(response, 500, "internal_error")
            assert envelope.run is None and envelope.validation == ()
            records = [record for record in caplog.records if getattr(record, "event_name", None) == "http_request_failed"]
            assert len(records) == 1
            assert records[0].public_error.model_dump() == envelope.error.model_dump()
            assert records[0].correlation.request_id == envelope.error.request_id
            assert records[0].correlation.error_id == envelope.error.error_id
            assert records[0].exception_type == "RuntimeError"
            assert SECRET in "".join(application_logs)
            completed = [record for record in caplog.records if getattr(record, "event_name", None) == "http_request_completed"]
            assert completed[-1].status_code == 500
            assert completed[-1].route == BASE

    run(exercise)


def test_http_exception_detail_is_not_public_and_allow_is_preserved(monkeypatch) -> None:
    async def exercise() -> None:
        async with http_application() as (app, client):
            async def fail(*args, **kwargs):
                raise HTTPException(405, detail={"secret": SECRET}, headers={"Allow": "POST"})

            monkeypatch.setattr(app.state.run_coordinator, "create_run", fail)
            response = await client.post(BASE, json={"query": SECRET})
            assert_error(response, 405, "method_not_allowed")
            assert response.headers["allow"] == "POST"

    run(exercise)


def test_closing_coordinator_returns_retryable_service_unavailable() -> None:
    async def exercise() -> None:
        async with http_application() as (app, client):
            await app.state.run_coordinator.shutdown()
            response = await client.post(BASE, json={"query": "query"})
            envelope = assert_error(response, 503, "service_unavailable")
            assert envelope.error.retryable is True
            assert app.state.run_coordinator._runs == {}

    run(exercise)


def test_request_ids_are_server_owned_isolated_and_inherited_by_workflows(caplog, application_logs) -> None:
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
            by_run = {UUID(response.json()["run_id"]): UUID(response.headers["x-request-id"]) for response in responses}
            assert len(set(by_run.values())) == 2
            assert UUID(spoofed) not in by_run.values()
            assert request_id_context.get() is None
            for context, release, request_id in contexts:
                assert request_id == by_run[context.run.run_id]
                release.set()
                stream = await client.get(f"{BASE}/{context.run.run_id}/events")
                assert UUID(stream.headers["x-request-id"]) not in by_run.values()
            lifecycle = [record for record in caplog.records if getattr(record, "event_name", None) == "lifecycle_event_committed"]
            assert len(lifecycle) == 4
            for record in lifecycle:
                assert record.correlation.request_id == by_run[record.correlation.run_id]
            health = await client.get("/health", headers={"X-Request-ID": SECRET})
            assert health.json() == {"status": "healthy"}
            assert health.headers["x-request-id"] != SECRET
            assert UUID(health.headers["x-request-id"])
            schema = await client.get("/openapi.json")
            assert UUID(schema.headers["x-request-id"])
            assert SECRET not in "".join(application_logs)

    run(exercise)


def test_started_stream_receives_workflow_failure_with_correlated_error(caplog, application_logs) -> None:
    caplog.set_level(logging.INFO)

    async def exercise() -> None:
        workflow = GatedWorkflow()
        async with http_application(workflow=workflow) as (app, client):
            created = await client.post(BASE, json={"query": SECRET})
            run_id = created.json()["run_id"]
            _, release, _ = await workflow.started.get()
            workflow.error = RuntimeError(SECRET)
            first_event = asyncio.Event()

            async def observe(message):
                if b"run_created" in message.get("body", b""):
                    first_event.set()

            task = asyncio.create_task(call_stream(app, f"{BASE}/{run_id}/events", send_hook=observe))
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
                assert [event["type"] for event in events] == ["run_created", "run_failed"]
                error = events[-1]["payload"]["error"]
                records = [record for record in caplog.records if getattr(record, "event_name", None) == "run_failed"]
                assert len(records) == 1
                assert str(records[0].correlation.error_id) == error["error_id"]
                assert str(records[0].correlation.request_id) == created.headers["x-request-id"]
                assert SECRET not in repr(messages)
                assert SECRET in "".join(application_logs)
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
            run_id = created.json()["run_id"]
            context, release, _ = await workflow.started.get()
            first_event = asyncio.Event()
            disconnect = asyncio.Event()

            async def observe(message):
                if b"run_created" in message.get("body", b""):
                    first_event.set()
                    if disconnect_mode == "write_failure":
                        raise OSError("connection closed")

            task = asyncio.create_task(call_stream(
                app, f"{BASE}/{run_id}/events", send_hook=observe, disconnect=disconnect,
                spec="2.3" if disconnect_mode == "disconnect" else "2.4",
            ))
            try:
                await first_event.wait()
                if disconnect_mode == "disconnect":
                    disconnect.set()
                    await task
                else:
                    with pytest.raises(RuntimeError, match="HTTP response stream aborted; request_id="):
                        await task
                runtime = app.state.run_coordinator._runs[UUID(run_id)]
                assert runtime.snapshot.status is RunStatus.RUNNING
                assert not context.cancellation.is_requested()
                assert runtime.workflow_task.cancelling() == 0
                release.set()
                replay = await client.get(f"{BASE}/{run_id}/events", headers={"Last-Event-ID": "1"})
                assert "run_completed" in replay.text
                assert "run_created" not in replay.text
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    run(exercise)


def test_openapi_describes_only_the_supported_routes_and_actual_envelopes() -> None:
    schema = create_app().openapi()
    paths = schema["paths"]
    assert {path: set(operations) for path, operations in paths.items()} == {
        "/health": {"get"}, BASE: {"post"}, BASE + "/{run_id}/events": {"get"},
        BASE + "/{run_id}/cancel": {"post"},
    }
    create = paths[BASE]["post"]
    assert "400" in create["responses"]
    assert create["responses"]["201"]["content"]["application/json"]["schema"]["$ref"].endswith("/AnalysisRunResponse")
    assert create["requestBody"]["required"] is True
    assert create["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/CreateAnalysisRunRequest")
    cancel = paths[BASE + "/{run_id}/cancel"]["post"]
    assert "requestBody" not in cancel
    events = paths[BASE + "/{run_id}/events"]["get"]
    assert "run" in events["description"]
    assert set(events["responses"]["200"]["content"]) == {"text/event-stream"}
    assert "content" not in events["responses"]["204"]
    assert any(parameter["in"] == "header" and parameter["name"] == "Last-Event-ID" for parameter in events["parameters"])
    assert set(schema["components"]["schemas"]["CreateAnalysisRunRequest"]["properties"]) == {"query", "time_resolution"}
    assert "HTTPValidationError" not in schema["components"]["schemas"]
    for path in paths.values():
        for operation in path.values():
            for status, response in operation["responses"].items():
                assert response["headers"]["X-Request-ID"]["schema"] == {"type": "string", "format": "uuid"}
                if int(status) >= 400:
                    assert response["content"]["application/json"]["schema"]["$ref"].endswith("/AnalysisRunErrorResponse")


def test_stream_keepalive_does_not_block_cancel_or_mix_request_contexts(caplog, application_logs) -> None:
    caplog.set_level(logging.INFO)

    async def exercise() -> None:
        workflow = GatedWorkflow()
        async with http_application(workflow=workflow, keepalive_interval=0.000001) as (app, client):
            created = await client.post(BASE, json={"query": "query"})
            run_id = created.json()["run_id"]
            _, release, _ = await workflow.started.get()
            comment_received = asyncio.Event()

            async def observe(message):
                if message.get("body") == b": keepalive\n\n":
                    comment_received.set()

            task = asyncio.create_task(call_stream(
                app, f"{BASE}/{run_id}/events", send_hook=observe,
                headers=[(b"last-event-id", b"1")],
            ))
            try:
                await comment_received.wait()
                cancelled = await client.post(f"{BASE}/{run_id}/cancel")
                assert cancelled.status_code == 200
                messages = await task
                release.set()
                header_id = dict(messages[0]["headers"])[b"x-request-id"].decode()
                assert len({header_id, cancelled.headers["x-request-id"], created.headers["x-request-id"]}) == 3
                assert [event["type"] for event in events_from(messages)] == ["run_cancelled"]
                assert b": keepalive\n\n" in frames_from(messages)
                completed = [record for record in caplog.records if getattr(record, "event_name", None) == "http_request_completed"]
                assert len(completed) == 3
                accepted = [record for record in caplog.records if getattr(record, "event_name", None) == "cancel_accepted"]
                assert str(accepted[0].correlation.request_id) == cancelled.headers["x-request-id"]
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    run(exercise)


def test_failure_after_stream_headers_never_appends_http_error_json(monkeypatch, application_logs) -> None:
    from app.api.sse import response as response_module

    async def exercise() -> None:
        workflow = GatedWorkflow()
        async with http_application(workflow=workflow) as (app, client):
            created = await client.post(BASE, json={"query": "query"})
            run_id = created.json()["run_id"]
            _, release, _ = await workflow.started.get()
            original_encode = response_module.encode_event

            def fail_terminal(event):
                if event.sequence == 2:
                    raise RuntimeError("encoder failure; api_key=" + SECRET)
                return original_encode(event)

            monkeypatch.setattr(response_module, "encode_event", fail_terminal)
            first_event = asyncio.Event()
            delivered = []

            async def observe(message):
                delivered.append(message)
                if message.get("body"):
                    first_event.set()

            task = asyncio.create_task(call_stream(app, f"{BASE}/{run_id}/events", send_hook=observe))
            try:
                await first_event.wait()
                release.set()
                with pytest.raises(RuntimeError, match="HTTP response stream aborted; request_id=") as raised:
                    await task
                assert SECRET not in str(raised.value)
                assert raised.value.__suppress_context__ is True
                assert raised.value.__cause__ is None
                assert [message["status"] for message in delivered if message["type"] == "http.response.start"] == [200]
                assert [event["type"] for event in events_from(delivered)] == ["run_created"]
                assert SECRET not in repr(delivered)
                logs = [json.loads(line) for line in application_logs]
                failure = next(row for row in logs if row["event_name"] == "http_stream_failed")
                assert failure["exception_type"] == "RuntimeError"
                assert failure["run_id"] == run_id
                assert failure["request_id"] == dict(delivered[0]["headers"])[b"x-request-id"].decode()
                assert str(raised.value) == f"HTTP response stream aborted; request_id={failure['request_id']}"
                assert failure["status_code"] == 200 and failure["level"] == "ERROR"
                assert SECRET not in "".join(application_logs)
                runtime = app.state.run_coordinator._runs[UUID(run_id)]
                assert runtime.snapshot.status is RunStatus.COMPLETED
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
