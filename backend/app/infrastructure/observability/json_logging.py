import json
import logging
import re
import sys
import traceback
from datetime import datetime, timezone
from enum import Enum
from uuid import UUID

from pydantic import BaseModel

from app.application.transcripts.redaction import (
    REDACTED,
    redact,
    redact_text,
    registered_private_values,
)

_FIELDS = (
    "operation", "from_status", "to_status", "method", "route", "status_code",
    "duration_ms", "exception_type", "public_error", "lifecycle_event",
    "original_status", "outcome", "attempt", "generation_status",
    "generation_age_seconds", "job_pending", "job_leased", "job_retry_wait",
    "job_failed", "job_lag_seconds", "embedding_stage", "error_code",
    "strategy", "candidate_count", "result_count", "active_profile_digest",
    "provider", "provider_operation", "provider_status", "provider_error",
    "provider_attempts", "provider_retry_after_seconds",
    "provider_rate_limit_limit", "provider_rate_limit_remaining",
    "provider_rate_limit_reset", "run_id", "session_id",
    "target_comid", "comid", "station_id", "series_id", "sample_identity",
    "parameter_code", "record_index", "validation_step",
    "failure_code", "model_calls",
    "stream_event_type", "stream_item_type", "stream_response_status",
    "stream_payload_keys", "stream_failure_rule", "stream_error_type",
    "stream_error_code",
    "schema_name", "schema_path", "schema_rule",
    "phase", "batch_id", "initial_operation_id", "repair_operation_id",
    "initial_issue_count", "repair_issue_count", "initial_issues", "repair_issues",
    "initial_output_chars", "repair_output_chars",
    "tool_name", "plan_index", "plan_count", "page_number", "response_bytes",
)
_FORMAT_FAILURE = (
    '{"timestamp":null,"level":"ERROR","event_name":"log_format_failed",'
    '"message":"Application log could not be formatted."}'
)
_PRIVATE_FRAGMENT = re.compile(r"[A-Za-z0-9._~+/_=\-]{8,}")


def _redact_known_fragments(value: object, private_values: tuple[str, ...]) -> object:
    """Hide long contiguous fragments of a known secret in log text."""
    if isinstance(value, str):
        return _PRIVATE_FRAGMENT.sub(
            lambda match: REDACTED if any(
                match.group() in secret for secret in private_values
            ) else match.group(),
            value,
        )
    if isinstance(value, dict):
        return {key: _redact_known_fragments(item, private_values)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_known_fragments(item, private_values) for item in value]
    return value


def _json_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return value


def _exception_fields(error: BaseException, private_values: tuple[str, ...]) -> dict[str, str]:
    summary = traceback.TracebackException.from_exception(
        error, lookup_lines=False, capture_locals=False
    )
    pending = [summary]
    while pending:
        current = pending.pop()
        current.stack = traceback.StackSummary.from_list([
            (frame.filename, frame.lineno, frame.name, "") for frame in current.stack
        ])
        pending.extend(item for item in (current.__cause__, current.__context__) if item)
        pending.extend(current.exceptions or ())
    return {
        "exception_type": type(error).__name__,
        "exception_message": redact_text(str(error), private_values),
        "stack_trace": redact_text("".join(summary.format()), private_values),
    }


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        try:
            private_values = (*registered_private_values(), *getattr(record, "redaction_values", ()))
            payload = {
                "timestamp": _json_value(getattr(
                    record, "timestamp", datetime.fromtimestamp(record.created, timezone.utc)
                )),
                "level": record.levelname,
                "event_name": getattr(record, "event_name", "application_log"),
                "message": record.getMessage(),
                "component": record.name,
            }
            correlation = getattr(record, "correlation", None)
            if correlation is not None:
                payload.update(correlation.model_dump(mode="json", exclude_none=True))
            for name in _FIELDS:
                value = getattr(record, name, None)
                if value is not None or (name == "from_status" and hasattr(record, name)):
                    payload[name] = _json_value(value)
            if record.exc_info and record.exc_info[1] is not None:
                payload.update(_exception_fields(
                    record.exc_info[1], private_values
                ))
            safe = redact(payload, private_values)
            event_name = payload["event_name"]
            if isinstance(event_name, str) and not any(
                secret in event_name for secret in private_values
            ):
                # Stable event identifiers can share a short word with an
                # unrelated key; keep the identifier if it has no full key.
                safe["event_name"] = redact_text(event_name)
            return json.dumps(
                _redact_known_fragments(safe, private_values),
                allow_nan=False, separators=(",", ":"),
            )
        except Exception:
            return _FORMAT_FAILURE


class JsonLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stderr.write(self.format(record) + "\n")
            sys.stderr.flush()
        except Exception:
            # Standard logging.handleError would print the unsanitized record to stderr.
            pass


def configure_logging(level: str) -> None:
    logger = logging.getLogger("wateragents")
    if not any(isinstance(handler, JsonLogHandler) for handler in logger.handlers):
        handler = JsonLogHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.setLevel(5 if level.lower() == "trace" else level.upper())
    logger.propagate = False
