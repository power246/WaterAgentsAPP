import io
import json
import logging
from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID

import pytest

from app.application.analysis_runs.models import CorrelationContext
from app.infrastructure.observability import json_logging
from app.infrastructure.observability.json_logging import (
    JsonFormatter,
    JsonLogHandler,
    configure_logging,
)
from app.infrastructure.observability.redaction import REDACTED, redact, redact_text


@pytest.mark.parametrize("name", [
    "Authorization", "pRoXy-aUtHoRiZaTiOn", "Cookie", "Set-Cookie", "X-API-Key",
    "api_key", "apiKey", "API KEY", "OPENAI_API_KEY", "clientSecret", "client_secret",
    "PASSWORD", "passwd", "access_token", "refreshToken", "AWS_ACCESS_KEY_ID",
    "aws_secret_access_key", "private_key", "Credential", "credentials",
    "request_body", "headers",
])
def test_nested_redaction_is_case_insensitive_and_does_not_mutate(name):
    source = {"items": [{name: {"nested": ["hidden-credential"]}, "count": 2}], "ok": True}
    before = deepcopy(source)
    assert redact(source) == {"items": [{name: REDACTED, "count": 2}], "ok": True}
    assert source == before


def test_repeated_values_keep_each_redaction_calls_private_scope():
    source = {"items": [{"name": "different-private-text", "value": "different-private-text"}] * 300}
    before = deepcopy(source)
    hidden = redact(source, ("different-private-text",))
    visible = redact(source)
    assert all(item == {"name": REDACTED, "value": REDACTED} for item in hidden["items"])
    assert visible == source == before


@pytest.mark.parametrize("text", [
    'Authorization: Bearer hidden-credential',
    'Cookie: sid=hidden-credential; preferences=hidden-credential',
    'Set-Cookie: sid=hidden-credential; HttpOnly',
    'Set-Cookie: sid=hidden-credential; Path=/, pref=hidden-credential; HttpOnly',
    'api_key="hidden-credential with spaces"',
    "{'client_secret': 'hidden-credential', 'safe': 4}",
    'https://provider.test/run?api_key=hidden-credential&count=3',
    'https://user:hidden-credential@provider.test/run',
    'upstream rejected bEaReR hidden-credential',
    'proxy rejected BASIC hidden-credential',
    'upstream rejected Bearer abcdefgh',
    'proxy rejected Basic dXNlcjpwYXNz',
    'password=hidden-credential\nnext safe line',
])
def test_credentials_in_diagnostic_text_are_redacted(text):
    result = redact_text(text)
    assert not any(
        credential in result
        for credential in ("hidden-credential", "abcdefgh", "dXNlcjpwYXNz")
    )
    assert REDACTED in result


def test_private_query_redacts_raw_repr_and_json_escapes():
    query = '私有地点\n"secret"'
    for rendered in (query, repr(query), json.dumps(query), json.dumps(query, ensure_ascii=False)):
        result = redact_text("failure: " + rendered, (query,))
        assert "secret" not in result
        assert REDACTED in result


def test_ordinary_query_is_not_treated_as_a_credential_container():
    source = {"query": "Lake Erie water level", "safe": True}

    assert redact(source) == source
    assert redact_text("query=Lake Erie water level") == "query=Lake Erie water level"


def test_formatter_has_stable_fields_and_ignores_unapproved_extras():
    record = logging.makeLogRecord({
        "name": "wateragents.http", "levelname": "INFO", "msg": "Handled %s.\nNext line.",
        "args": ("request",), "event_name": "http_request_completed",
        "timestamp": datetime(2026, 9, 8, tzinfo=timezone.utc),
        "correlation": CorrelationContext(request_id=UUID(int=1)),
        "method": "GET", "route": "/health", "status_code": 200, "duration_ms": 12.5,
        "body": "hidden-body", "headers": {"Cookie": "hidden-cookie"},
        "query": "hidden-query", "random_object": object(),
    })
    line = JsonFormatter().format(record)
    assert len(line.splitlines()) == 1
    assert "hidden-" not in line
    assert json.loads(line) == {
        "timestamp": "2026-09-08T00:00:00Z", "level": "INFO",
        "event_name": "http_request_completed", "message": "Handled request.\nNext line.",
        "component": "wateragents.http", "request_id": str(UUID(int=1)),
        "method": "GET", "route": "/health", "status_code": 200, "duration_ms": 12.5,
    }


def test_provider_and_summary_failures_keep_safe_diagnostic_fields():
    provider = logging.makeLogRecord({
        "msg": "provider_request_failed", "provider": "usgs",
        "provider_operation": "collect", "provider_status": 503,
        "provider_error": "PortUnavailableError", "provider_attempts": 3,
        "headers": {"Authorization": "Bearer hidden"},
    })
    summary = logging.makeLogRecord({
        "msg": "run_summary_model_failed", "run_id": str(UUID(int=1)),
        "session_id": str(UUID(int=2)), "failure_code": "waterbody_model_timeout",
        "model_calls": 2,
    })
    provider_json = json.loads(JsonFormatter().format(provider))
    summary_json = json.loads(JsonFormatter().format(summary))
    assert provider_json["provider_status"] == 503
    assert provider_json["provider_attempts"] == 3
    assert provider_json["provider_error"] == "PortUnavailableError"
    assert "headers" not in provider_json
    assert summary_json["run_id"] == str(UUID(int=1))
    assert summary_json["session_id"] == str(UUID(int=2))
    assert summary_json["failure_code"] == "waterbody_model_timeout"
    assert summary_json["model_calls"] == 2


def test_provider_retry_after_seconds_is_written_without_raw_header():
    record = logging.makeLogRecord({
        "msg": "provider_request_failed", "provider": "usgs_nldi",
        "provider_status": 429, "provider_retry_after_seconds": 153.0,
        "headers": {"Retry-After": "hidden-raw-header"},
    })
    payload = json.loads(JsonFormatter().format(record))
    assert payload["provider_retry_after_seconds"] == 153.0
    assert "headers" not in payload
    assert "hidden-raw-header" not in json.dumps(payload)


def test_provider_validation_context_is_written_without_response_content():
    record = logging.makeLogRecord({
        "msg": "provider_observation_invalid", "provider": "usgs",
        "comid": "1777616", "station_id": "USGS-123", "series_id": "series-123",
        "sample_identity": "sample-123", "parameter_code": "00011",
        "page_number": 2, "record_index": 17, "validation_step": "value",
        "response_body": "hidden-response",
    })
    payload = json.loads(JsonFormatter().format(record))
    for field in (
        "comid", "station_id", "series_id", "sample_identity",
        "parameter_code", "page_number", "record_index", "validation_step",
    ):
        assert payload[field] == getattr(record, field)
    assert "response_body" not in payload
    assert "hidden-response" not in json.dumps(payload)


def test_reasoning_repair_failure_keeps_issue_summary_without_model_content():
    record = logging.makeLogRecord({
        "msg": "reasoning_validation_repair_failed",
        "run_id": str(UUID(int=1)), "session_id": str(UUID(int=2)),
        "target_comid": "1777622", "phase": "reasoning_batch",
        "batch_id": "batch-1", "initial_operation_id": str(UUID(int=3)),
        "repair_operation_id": str(UUID(int=4)),
        "initial_issue_count": 2, "repair_issue_count": 1,
        "initial_issues": [{"code": "invalid_value", "path": "$.cells[0]"}],
        "repair_issues": [{"code": "missing_field", "path": "$.cells[0].source"}],
        "initial_output_chars": 120, "repair_output_chars": 80,
        "response_body": "hidden-model-output",
    })
    payload = json.loads(JsonFormatter().format(record))
    assert payload["phase"] == "reasoning_batch"
    assert payload["initial_issue_count"] == 2
    assert payload["repair_issues"] == [
        {"code": "missing_field", "path": "$.cells[0].source"}
    ]
    assert payload["repair_output_chars"] == 80
    assert "hidden-model-output" not in json.dumps(payload)


def test_stream_protocol_diagnostics_are_serialized_without_response_content():
    record = logging.makeLogRecord({
        "msg": "provider_stream_protocol_invalid",
        "provider": "openai",
        "stream_event_type": "response.completed",
        "stream_payload_keys": ["response", "type"],
        "stream_failure_rule": "_terminal_events:1000",
        "response_content": "hidden-response",
    })
    payload = json.loads(JsonFormatter().format(record))
    assert payload["stream_event_type"] == "response.completed"
    assert payload["stream_payload_keys"] == ["response", "type"]
    assert payload["stream_failure_rule"] == "_terminal_events:1000"
    assert "hidden-response" not in json.dumps(payload)


@pytest.mark.parametrize("chain", ["cause", "context", "group"])
def test_exception_chain_keeps_diagnostics_without_source_lines_or_locals(chain):
    try:
        try:
            raise ValueError("provider timeout; api_key=hidden-credential")
        except ValueError as cause:
            if chain == "group":
                raise ExceptionGroup("parallel failure", [cause, OSError("provider unavailable")])
            if chain == "cause":
                raise RuntimeError("analysis failed") from cause
            raise RuntimeError("analysis failed")
    except Exception as error:
        record = logging.makeLogRecord({"msg": "Workflow failed.", "exc_info": (type(error), error, error.__traceback__)})
    line = JsonFormatter().format(record)
    payload = json.loads(line)
    assert "hidden-credential" not in line
    assert "raise ValueError" not in line
    assert "provider timeout" in payload["stack_trace"]
    assert "ValueError" in payload["stack_trace"]
    assert "test_exception_chain_keeps_diagnostics" in payload["stack_trace"]
    assert "File" in payload["stack_trace"]
    assert payload["exception_type"] == ("ExceptionGroup" if chain == "group" else "RuntimeError")


@pytest.mark.parametrize("failure", ["redactor", "model", "message", "nonfinite", "cycle"])
def test_formatter_failure_emits_only_safe_json(monkeypatch, failure):
    class Broken:
        def __str__(self):
            raise RuntimeError("hidden-formatter-secret")

    record = logging.makeLogRecord({"msg": "hidden-raw-message"})
    if failure == "redactor":
        monkeypatch.setattr(json_logging, "redact", lambda value: (_ for _ in ()).throw(ValueError("hidden-redactor-secret")))
    elif failure == "model":
        record.correlation = object()
    elif failure == "message":
        record.msg = Broken()
    elif failure == "nonfinite":
        record.duration_ms = float("nan")
    else:
        record.public_error = {}
        record.public_error["cycle"] = record.public_error
    line = JsonFormatter().format(record)
    assert json.loads(line)["event_name"] == "log_format_failed"
    assert "hidden-" not in line


def test_handler_does_not_print_raw_logging_errors(monkeypatch, capsys):
    handler = JsonLogHandler()
    formatter = JsonFormatter()
    handler.setFormatter(formatter)

    def broken(record):
        raise RuntimeError("hidden-format-secret")

    monkeypatch.setattr(formatter, "format", broken)
    handler.handle(logging.makeLogRecord({"msg": "hidden-raw-message"}))
    assert capsys.readouterr().err == ""


def test_json_output_survives_non_utf8_console_encoding(monkeypatch):
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp1252", errors="strict")
    monkeypatch.setattr(json_logging.sys, "stderr", stream)
    handler = JsonLogHandler()
    handler.setFormatter(JsonFormatter())
    handler.handle(logging.makeLogRecord({"msg": "阶段完成 💧\nNext line."}))
    lines = buffer.getvalue().decode("ascii").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["message"] == "阶段完成 💧\nNext line."


@pytest.mark.parametrize("level,expected", [("info", 20), ("warning", 30), ("trace", 5)])
def test_configuration_is_scoped_idempotent_and_respects_settings(level, expected, application_logs):
    root = logging.getLogger()
    uvicorn = logging.getLogger("uvicorn")
    before = (root.handlers[:], root.level, uvicorn.handlers[:], uvicorn.level)
    configure_logging(level)
    configure_logging(level)
    logger = logging.getLogger("wateragents")
    assert len([handler for handler in logger.handlers if isinstance(handler, JsonLogHandler)]) == 1
    assert logger.level == expected
    assert logger.propagate is False
    assert before == (root.handlers[:], root.level, uvicorn.handlers[:], uvicorn.level)
