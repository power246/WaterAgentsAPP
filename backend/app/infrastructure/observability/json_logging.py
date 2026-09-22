import json
import logging
import sys
import traceback
from datetime import datetime, timezone
from enum import Enum
from uuid import UUID

from pydantic import BaseModel

from app.infrastructure.observability.redaction import redact, redact_text

_FIELDS = (
    "operation", "from_status", "to_status", "method", "route", "status_code",
    "duration_ms", "exception_type", "public_error", "lifecycle_event",
    "original_status", "outcome", "attempt",
)
_FORMAT_FAILURE = (
    '{"timestamp":null,"level":"ERROR","event_name":"log_format_failed",'
    '"message":"Application log could not be formatted."}'
)


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
                    record.exc_info[1], getattr(record, "redaction_values", ())
                ))
            return json.dumps(redact(payload), allow_nan=False, separators=(",", ":"))
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
