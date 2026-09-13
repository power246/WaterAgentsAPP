import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
import pytest

from app.application.analysis_runs.models import RunStatus
from app.application.correlation import request_id_context
from app.infrastructure.observability import json_logging
from app.infrastructure.observability.json_logging import JsonFormatter
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from app.main import create_app

BASE = "/api/analysis/runs"
QUERY = 'private-location\n"机密"'
SECRET = "test-private-credential"


class Clock:
    def __init__(self):
        self.instant = datetime(2026, 9, 8, tzinfo=timezone.utc)

    def now(self):
        return self.instant


class Workflow:
    def __init__(self):
        self.calls = asyncio.Queue()

    async def run(self, context):
        release = asyncio.Event()
        self.calls.put_nowait((context, release))
        await release.wait()
        try:
            raise OSError(f"provider timed out; Authorization: Bearer {SECRET}")
        except OSError as cause:
            raise RuntimeError(f"analysis failed for {context.query}") from cause


@asynccontextmanager
async def application(*, workflow=None, clock=None):
    app = create_app(
        clock=clock or Clock(), workflow=workflow, shutdown_grace_period=0,
        transcript_store=InMemoryTranscriptStore(),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield app, client
    assert request_id_context.get() is None
    assert all(runtime.workflow_task.done() for runtime in app.state.run_coordinator._runs.values())


def run(exercise):
    asyncio.run(asyncio.wait_for(exercise(), 5))


def parsed(lines, event_name=None):
    payloads = [json.loads(line) for line in lines]
    assert all("\n" not in line for line in lines)
    assert all({"timestamp", "level", "event_name", "message"} <= payload.keys() for payload in payloads)
    return [row for row in payloads if event_name is None or row["event_name"] == event_name]


def test_completed_lifecycle_json_matches_sse_and_original_objects(application_logs, caplog, capsys):
    async def exercise():
        async with application() as (app, client):
            created = await client.post(BASE, json={"query": QUERY}, headers={"Authorization": SECRET, "Cookie": SECRET})
            run_id = created.json()["run_id"]
            stream = await client.get(f"{BASE}/{run_id}/events")
            events = [json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: ")]
            assert [event["type"] for event in events] == ["run_created", "run_completed"]
            logs = parsed(application_logs, "lifecycle_event_committed")
            assert [row["lifecycle_event"] for row in logs] == events
            assert [row["event_sequence"] for row in logs] == [1, 2]
            assert [row["timestamp"] for row in logs] == [event["occurred_at"] for event in events]
            assert all(row["request_id"] == created.headers["x-request-id"] for row in logs)
            assert all(row["run_id"] == run_id for row in logs)
            records = [record for record in caplog.records if getattr(record, "lifecycle_event", None)]
            assert [record.lifecycle_event.model_dump(mode="json") for record in records] == events
            committed = app.state.run_coordinator._runs[UUID(run_id)].events
            assert all(record.lifecycle_event is event for record, event in zip(records, committed, strict=True))
            states = [row for row in parsed(application_logs) if "to_status" in row]
            assert [(row["from_status"], row["to_status"]) for row in states] == [
                (None, "pending"), ("pending", "running"), ("running", "completed")
            ]
            assert not parsed(application_logs, "run_started")
            for response, method, route, status in (
                (created, "POST", BASE, 201), (stream, "GET", BASE + "/{run_id}/events", 200)
            ):
                row = next(row for row in parsed(application_logs, "http_request_completed") if row["request_id"] == response.headers["x-request-id"])
                assert (row["method"], row["route"], row["status_code"]) == (method, route, status)
                assert row["duration_ms"] >= 0
            assert QUERY not in created.text + stream.text
            assert SECRET not in "".join(application_logs)
            assert "private-location" not in "".join(application_logs)

    run(exercise)
    emitted = capsys.readouterr().err.splitlines()
    assert len(emitted) == len(application_logs)
    assert [json.loads(line) for line in emitted] == parsed(application_logs)


def test_failure_error_id_diagnostics_and_request_context_survive_concurrent_requests(application_logs):
    async def exercise():
        workflow = Workflow()
        async with application(workflow=workflow) as (_, client):
            created = await asyncio.gather(*[
                client.post(BASE, json={"query": f"{QUERY} {index}"}) for index in range(2)
            ])
            calls = [await workflow.calls.get() for _ in created]
            for _, release in calls:
                release.set()
            for index, response in enumerate(created):
                run_id = response.json()["run_id"]
                stream = await client.get(f"{BASE}/{run_id}/events")
                event = json.loads(next(line[6:] for line in stream.text.splitlines() if line.startswith("data: ") and '"run_failed"' in line))
                row = next(row for row in parsed(application_logs, "run_failed") if row["run_id"] == run_id)
                assert row["error_id"] == event["payload"]["error"]["error_id"]
                assert row["request_id"] == response.headers["x-request-id"]
                assert row["request_id"] != stream.headers["x-request-id"]
                assert row["level"] == "ERROR"
                assert row["exception_type"] == "RuntimeError"
                assert row["exception_message"] == f"analysis failed for {QUERY} {index}"
                assert "OSError" in row["stack_trace"] and "direct cause" in row["stack_trace"]
                assert "provider timed out" in row["stack_trace"]
                assert "raise OSError" not in row["stack_trace"]
                assert "stack_trace" not in stream.text and "test_log_correlation.py" not in stream.text
            assert SECRET not in "".join(application_logs)
            assert "private-location" in "".join(application_logs)

    run(exercise)


@pytest.mark.parametrize("elapsed_ms", [125, -125])
def test_http_duration_uses_clock_and_server_errors_keep_safe_diagnostics(monkeypatch, application_logs, elapsed_ms):
    async def exercise():
        clock = Clock()
        async with application(clock=clock) as (app, client):
            async def fail(*args, **kwargs):
                clock.instant += timedelta(milliseconds=elapsed_ms)
                raise RuntimeError(f"service failure {QUERY}; API_KEY={SECRET}")

            monkeypatch.setattr(app.state.run_coordinator, "create_run", fail)
            response = await client.post(BASE + "?token=" + SECRET, json={"query": QUERY})
            assert response.status_code == 500
            public = response.json()["error"]
            error = parsed(application_logs, "http_request_failed")[-1]
            completed = parsed(application_logs, "http_request_completed")[-1]
            assert public["error_id"] == error["error_id"]
            assert response.headers["x-request-id"] == error["request_id"] == completed["request_id"]
            assert completed["duration_ms"] == max(0, elapsed_ms)
            assert completed["route"] == BASE
            assert error["exception_type"] == "RuntimeError"
            assert "service failure" in error["exception_message"]
            assert "test_log_correlation.py" in error["stack_trace"]
            assert SECRET not in "".join(application_logs) + response.text
            assert "private-location" in "".join(application_logs)
            assert "private-location" not in response.text
            assert "test_log_correlation.py" not in response.text

    run(exercise)


def test_validation_and_unknown_paths_never_echo_inputs_in_json_logs(application_logs):
    async def exercise():
        async with application() as (_, client):
            invalid = await client.post(BASE, json={"query": {"password": SECRET}, SECRET: QUERY}, headers={"Authorization": SECRET, "Cookie": SECRET})
            unknown = await client.get("/" + SECRET, params={"query": QUERY})
            assert invalid.status_code == 422 and unknown.status_code == 404
            for row in parsed(application_logs, "http_request_failed"):
                assert "stack_trace" not in row and "exception_message" not in row
            assert SECRET not in "".join(application_logs) + invalid.text + unknown.text
            assert "private-location" not in "".join(application_logs)

    run(exercise)


def test_cancelled_late_exception_keeps_diagnostics_without_a_failed_event(application_logs):
    async def exercise():
        workflow = Workflow()
        async with application(workflow=workflow) as (app, client):
            created = await client.post(BASE, json={"query": QUERY})
            run_id = created.json()["run_id"]
            _, release = await workflow.calls.get()
            cancelled = await client.post(f"{BASE}/{run_id}/cancel")
            release.set()
            runtime = app.state.run_coordinator._runs[UUID(run_id)]
            await runtime.workflow_quiesced.wait()
            stream = await client.get(f"{BASE}/{run_id}/events")
            assert "run_cancelled" in stream.text and "run_failed" not in stream.text
            suppressed = parsed(application_logs, "workflow_output_suppressed")[-1]
            assert suppressed["exception_type"] == "RuntimeError"
            assert "error_id" not in suppressed
            assert suppressed["request_id"] == created.headers["x-request-id"]
            assert parsed(application_logs, "cancel_accepted")[-1]["request_id"] == cancelled.headers["x-request-id"]
            assert len(parsed(application_logs, "workflow_quiesced")) == 1
            assert not parsed(application_logs, "run_failed")
            assert SECRET not in "".join(application_logs)
            assert "private-location" in "".join(application_logs)

    run(exercise)


@pytest.mark.parametrize("failure", ["redactor", "formatter", "writer"])
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_observability_failure_does_not_change_http_or_run_outcomes(monkeypatch, failure, outcome, application_logs):
    def broken(*args, **kwargs):
        raise RuntimeError("private-observability-failure")

    async def exercise():
        workflow = None if outcome == "completed" else Workflow()
        async with application(workflow=workflow) as (app, client):
            if failure == "redactor":
                monkeypatch.setattr(json_logging, "redact", broken)
            elif failure == "formatter":
                monkeypatch.setattr(JsonFormatter, "format", broken)
            else:
                class BrokenWriter:
                    write = broken
                    flush = broken
                monkeypatch.setattr(json_logging.sys, "stderr", BrokenWriter())
            created = await client.post(BASE, json={"query": QUERY})
            assert created.status_code == 201
            assert UUID(created.headers["x-request-id"])
            run_id = created.json()["run_id"]
            if workflow is not None:
                _, release = await workflow.calls.get()
                if outcome == "cancelled":
                    cancelled = await client.post(f"{BASE}/{run_id}/cancel")
                    assert cancelled.status_code == 200
                release.set()
            stream = await client.get(f"{BASE}/{run_id}/events")
            assert stream.status_code == 200
            assert f'"run_{outcome}"' in stream.text
            runtime = app.state.run_coordinator._runs[UUID(run_id)]
            await runtime.workflow_quiesced.wait()
            assert runtime.snapshot.status is RunStatus(outcome)
            assert len(runtime.events) == 2
            assert (await client.post(BASE, json={"query": None})).status_code == 422
            if failure == "redactor":
                assert all(row["event_name"] == "log_format_failed" for row in parsed(application_logs))
            assert "private-observability-failure" not in created.text + stream.text

    run(exercise)
