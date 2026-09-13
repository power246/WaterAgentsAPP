import logging
from datetime import datetime
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from app.api.schemas.analysis_runs import (
    AnalysisRunErrorResponse,
    AnalysisRunResponse,
    CreateAnalysisRunRequest,
    HttpPublicError,
    ValidationViolation,
)
from app.application.analysis_runs.coordinator import (
    EventCursorAheadError,
    InvalidEventCursorError,
    InvalidRunTransitionError,
    ServiceShuttingDownError,
    RunNotFoundError,
)
from app.application.analysis_runs.models import CorrelationContext, RunStatus
from app.domain.common import normalize_utc_datetime

_VALIDATION_MESSAGES = {
    "missing": "This field is required.",
    "string_type": "Expected a string.",
    "string_too_short": "The value must not be empty.",
    "enum": "Select a supported value.",
    "extra_forbidden": "Unknown fields are not allowed.",
    "uuid_parsing": "Expected a valid UUID.",
    "uuid_type": "Expected a valid UUID.",
    "json_invalid": "The request body must contain valid JSON.",
    "model_attributes_type": "Expected a JSON object.",
    "model_type": "Expected a JSON object.",
    "value_error": "The value is invalid.",
}
_SAFE_FIELDS = set(CreateAnalysisRunRequest.model_fields) | {
    "body", "path", "header", "run_id", "last-event-id"
}


def request_run_id(request: Request) -> UUID | None:
    value = getattr(request.state, "run_id", None) or request.path_params.get("run_id")
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            pass
    return None


def log_http(
    request: Request,
    event_name: str,
    status_code: int,
    *,
    error: HttpPublicError | None = None,
    exception_type: str | None = None,
    exception: BaseException | None = None,
    started_at: datetime | None = None,
) -> None:
    try:
        timestamp = normalize_utc_datetime(request.app.state.clock.now())
        logging.getLogger("wateragents.http").log(
            logging.ERROR if status_code >= 500 or exception is not None else logging.INFO,
            "HTTP request handled.",
            exc_info=(type(exception), exception, exception.__traceback__)
            if exception is not None else None,
            extra={
                "event_name": event_name,
                "timestamp": timestamp,
                "correlation": CorrelationContext(
                    request_id=request.state.request_id,
                    run_id=request_run_id(request),
                    error_id=error.error_id if error else None,
                ),
                "method": request.method,
                "route": getattr(request.scope.get("route"), "path", None),
                "status_code": status_code,
                "public_error": error,
                "exception_type": exception_type,
                "redaction_values": (getattr(request.state, "log_private_query", ""),)
                if exception is not None else (),
                "duration_ms": max(0.0, (timestamp - started_at).total_seconds() * 1000)
                if started_at is not None else None,
            },
        )
    except Exception:
        pass


def validation_violations(error: RequestValidationError) -> tuple[ValidationViolation, ...]:
    violations = []
    for item in error.errors():
        field = tuple(
            part if type(part) is int or part in _SAFE_FIELDS else "unknown_field"
            for part in item["loc"]
        )
        error_type = item["type"] if item["type"] in _VALIDATION_MESSAGES else "invalid_value"
        message = _VALIDATION_MESSAGES.get(error_type, "The value is invalid.")
        if error_type == "string_too_short" and field == ("body", "query"):
            message = "The query must not be empty."
        violations.append(ValidationViolation(field=field, type=error_type, message=message))
    return tuple(violations)


async def handle_error(request: Request, exception: Exception) -> JSONResponse:
    status, code, message = 500, "internal_error", "The request could not be completed."
    stage = None
    retryable = False
    run = None
    validation = ()
    headers = {}
    if isinstance(exception, RequestValidationError):
        status, code, message = 422, "invalid_request", "The request is invalid."
        validation = validation_violations(exception)
    elif isinstance(exception, RunNotFoundError):
        status, code, message = 404, "run_not_found", "The run was not found."
    elif isinstance(exception, InvalidEventCursorError):
        status, code = 400, "invalid_event_cursor"
        message = "The event cursor must be a non-negative decimal integer."
        stage = "subscribe_events"
    elif isinstance(exception, EventCursorAheadError):
        status, code = 409, "event_cursor_ahead"
        message = "The event cursor is ahead of the latest run event."
        stage = "subscribe_events"
    elif (
        isinstance(exception, InvalidRunTransitionError)
        and exception.target_status is RunStatus.CANCELLED
    ):
        status, code = 409, "run_already_terminal"
        message = f"The run is already {exception.run.status.value}."
        stage = "cancel_run"
        run = AnalysisRunResponse.model_validate(exception.run, from_attributes=True)
    elif isinstance(exception, ServiceShuttingDownError):
        status, code, message = 503, "service_unavailable", "The service is shutting down."
        retryable = True
    elif isinstance(exception, HTTPException):
        status = exception.status_code
        code, message = {
            400: ("invalid_request", "The request is invalid."),
            404: ("not_found", "The requested resource was not found."),
            405: ("method_not_allowed", "The HTTP method is not allowed."),
            422: ("invalid_request", "The request is invalid."),
            500: ("internal_error", "The request could not be completed."),
        }.get(status, ("http_error", "The HTTP request could not be completed."))
        headers.update(exception.headers or {})
    error = HttpPublicError(
        error_id=request.app.state.id_factory(),
        request_id=request.state.request_id,
        run_id=request_run_id(request),
        code=code,
        message=message,
        failed_stage=stage,
        retryable=retryable,
    )
    envelope = AnalysisRunErrorResponse(error=error, run=run, validation=validation)
    headers["X-Request-ID"] = str(error.request_id)
    log_http(
        request, "http_request_failed", status, error=error,
        exception_type=type(exception).__name__,
        exception=exception if status >= 500 and not isinstance(exception, HTTPException) else None,
    )
    return JSONResponse(envelope.model_dump(mode="json"), status_code=status, headers=headers)


def register_error_handlers(application: FastAPI) -> None:
    for exception_type in (
        RequestValidationError, HTTPException, RunNotFoundError, InvalidEventCursorError,
        EventCursorAheadError, InvalidRunTransitionError, ServiceShuttingDownError,
    ):
        application.add_exception_handler(exception_type, handle_error)
