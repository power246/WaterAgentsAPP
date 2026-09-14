from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import TypeAdapter, ValidationError
from starlette.responses import Response

from app.api.schemas.analysis_runs import (
    AnalysisRunErrorResponse,
    AnalysisRunResponse,
    CreateAnalysisRunRequest,
    PersistenceRetryResponse,
    RunDetailResponse,
    RunHistoryResponse,
    RunStatusResponse,
    SessionDetailResponse,
    SessionTranscriptResponse,
    TranscriptContentResponse,
)
from app.api.http.errors import UnknownQueryParametersError
from app.api.sse.response import (
    create_run_event_response,
    create_session_event_response,
)
from app.application.analysis_runs.models import AnalysisRunSnapshot, RunStatus
from app.application.analysis_runs.persistence import PersistenceState
from app.application.analysis_runs.creation import InvalidIdempotencyKeyError
from app.application.analysis_runs.coordinator import (
    InvalidRunTransitionError,
    RunNotFoundError,
)
from app.application.transcripts.read_models import ReadView
from app.application.transcripts.reader import (
    PersistenceRetryUnavailableError,
    RunAlreadyTerminalError,
    RunNotLiveError,
)

_UUID = TypeAdapter(UUID)
_UUID_V4_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
_ASCII_DECIMAL_PATTERN = r"^[0-9]+$"

REQUEST_ID_HEADERS = {
    "X-Request-ID": {
        "description": "Server-generated request UUID.",
        "schema": {"type": "string", "format": "uuid"},
    }
}


def error_responses(*statuses: int) -> dict[int, dict[str, object]]:
    return {
        status: {"model": AnalysisRunErrorResponse, "headers": REQUEST_ID_HEADERS}
        for status in statuses
    }


router = APIRouter(
    prefix="/api/analysis/runs",
    tags=["analysis-runs"],
    responses=error_responses(405, 422, 500, 503),
)


@router.post(
    "", status_code=201, response_model=AnalysisRunResponse,
    responses={
        **error_responses(400, 409),
        201: {"headers": REQUEST_ID_HEADERS},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "$ref": "#/components/schemas/CreateAnalysisRunRequest"
                    }
                }
            },
        }
    },
)
async def create_run(
    request: Request,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            description="Optional client-generated UUID v4, reused with identical bytes.",
            json_schema_extra={
                "format": "uuid",
                "pattern": _UUID_V4_PATTERN,
            },
        ),
    ] = None,
) -> AnalysisRunSnapshot:
    _reject_unknown_query_parameters(request, set())
    key = None
    if idempotency_key is not None:
        try:
            key = _UUID.validate_python(idempotency_key)
        except ValidationError as error:
            raise InvalidIdempotencyKeyError() from error
        if key.version != 4:
            raise InvalidIdempotencyKeyError()
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type != "application/json":
        try:
            CreateAnalysisRunRequest.model_validate(None)
        except ValidationError as error:
            raise RequestValidationError(
                _body_validation_errors(error),
                body=None,
            ) from error
    raw_body = await request.body()
    try:
        snapshot = await request.app.state.run_coordinator.create_run(
            raw_body,
            idempotency_key=key,
        )
    except ValidationError as error:
        raise RequestValidationError(
            _body_validation_errors(error),
            body=None,
        ) from error
    request.state.run_id = snapshot.run_id
    return snapshot


def _body_validation_errors(error: ValidationError) -> list[dict[str, object]]:
    return [
        {**item, "loc": ("body", *item["loc"]), "input": None}
        for item in error.errors()
    ]


def _reject_unknown_query_parameters(
    request: Request,
    allowed: set[str],
) -> None:
    unknown = tuple(
        dict.fromkeys(key for key in request.query_params if key not in allowed)
    )
    if unknown:
        raise UnknownQueryParametersError(unknown)


@router.get(
    "",
    response_model=RunHistoryResponse,
    responses={200: {"headers": REQUEST_ID_HEADERS}},
)
async def list_runs(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> RunHistoryResponse:
    _reject_unknown_query_parameters(request, {"limit", "cursor"})
    return await request.app.state.transcript_reader.list_runs(
        limit=limit,
        cursor=cursor,
    )


@router.get(
    "/{run_id}",
    response_model=RunDetailResponse,
    responses={**error_responses(404), 200: {"headers": REQUEST_ID_HEADERS}},
)
async def get_run(
    request: Request,
    run_id: UUID,
    view: ReadView = ReadView.AUTO,
) -> RunDetailResponse:
    _reject_unknown_query_parameters(request, {"view"})
    request.state.run_id = run_id
    return await request.app.state.transcript_reader.get_run(run_id, view)


@router.get(
    "/{run_id}/sessions/{session_id}",
    response_model=SessionDetailResponse,
    responses={**error_responses(404), 200: {"headers": REQUEST_ID_HEADERS}},
)
async def get_session(
    request: Request,
    run_id: UUID,
    session_id: UUID,
    view: ReadView = ReadView.AUTO,
) -> SessionDetailResponse:
    _reject_unknown_query_parameters(request, {"view"})
    request.state.run_id = run_id
    request.state.session_id = session_id
    return await request.app.state.transcript_reader.get_session(
        run_id,
        session_id,
        view,
    )


@router.get(
    "/{run_id}/sessions/{session_id}/transcript",
    response_model=SessionTranscriptResponse,
    responses={**error_responses(404), 200: {"headers": REQUEST_ID_HEADERS}},
)
async def get_session_transcript(
    request: Request,
    run_id: UUID,
    session_id: UUID,
    view: ReadView = ReadView.AUTO,
) -> SessionTranscriptResponse:
    _reject_unknown_query_parameters(request, {"view"})
    request.state.run_id = run_id
    request.state.session_id = session_id
    return await request.app.state.transcript_reader.get_session_transcript(
        run_id,
        session_id,
        view,
    )


@router.get(
    "/{run_id}/transcript/{item_id}/content",
    response_model=TranscriptContentResponse,
    responses={**error_responses(404), 200: {"headers": REQUEST_ID_HEADERS}},
)
async def get_transcript_content(
    request: Request,
    run_id: UUID,
    item_id: UUID,
    view: ReadView = ReadView.AUTO,
) -> TranscriptContentResponse:
    _reject_unknown_query_parameters(request, {"view"})
    request.state.run_id = run_id
    return await request.app.state.transcript_reader.get_content(
        run_id,
        item_id,
        view,
    )


@router.get(
    "/{run_id}/events", response_class=Response,
    description=(
        "Sends the latest complete lightweight run snapshot, then replacement snapshots "
        "when source state changes. Last-Event-ID is ignored. Normal completion emits "
        "stream_end; disconnecting does not cancel the run."
    ),
    responses={
        **error_responses(404, 410),
        200: {
            "description": "Run snapshot SSE stream with comment keepalive.",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
            "headers": REQUEST_ID_HEADERS,
        },
    },
)
async def subscribe_events(
    request: Request,
    run_id: UUID,
) -> Response:
    _reject_unknown_query_parameters(request, set())
    request.state.run_id = run_id
    return await create_run_event_response(
        request.app.state.run_coordinator,
        request.app.state.transcript_reader,
        run_id,
    )


@router.get(
    "/{run_id}/sessions/{session_id}/events",
    response_class=Response,
    description=(
        "Replays visible Agent-session process events after the session-scoped integer "
        "Last-Event-ID, then streams new events. Normal completion emits stream_end."
    ),
    responses={
        **error_responses(400, 404, 409, 410),
        200: {
            "description": "Session process SSE stream with comment keepalive.",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
            "headers": REQUEST_ID_HEADERS,
        },
    },
)
async def subscribe_session_events(
    request: Request,
    run_id: UUID,
    session_id: UUID,
    last_event_id: Annotated[
        str | None,
        Header(
            alias="Last-Event-ID",
            description="Non-negative ASCII decimal session event cursor.",
            json_schema_extra={"pattern": _ASCII_DECIMAL_PATTERN},
        ),
    ] = None,
) -> Response:
    _reject_unknown_query_parameters(request, set())
    request.state.run_id = run_id
    request.state.session_id = session_id
    return await create_session_event_response(
        request.app.state.run_coordinator,
        request.app.state.transcript_reader,
        run_id,
        session_id,
        last_event_id=last_event_id,
    )


@router.post(
    "/{run_id}/cancel", response_model=RunStatusResponse,
    responses={**error_responses(404, 409), 200: {"headers": REQUEST_ID_HEADERS}},
)
async def cancel_run(request: Request, run_id: UUID) -> RunStatusResponse:
    _reject_unknown_query_parameters(request, set())
    request.state.run_id = run_id
    coordinator = request.app.state.run_coordinator
    try:
        snapshot = await coordinator.cancel_run(run_id)
    except InvalidRunTransitionError as error:
        persistence = await coordinator.get_persistence_info(run_id)
        raise RunAlreadyTerminalError(RunStatusResponse(
            run_id=error.run.run_id,
            status=error.run.status,
            created_at=error.run.created_at,
            updated_at=error.run.updated_at,
            persistence=persistence,
        )) from error
    except RunNotFoundError:
        historical = await request.app.state.transcript_reader.get_historical_status(
            run_id
        )
        if historical.status is RunStatus.CANCELLED:
            return historical
        if historical.status in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.COMPLETED_WITH_PERSISTENCE_ERROR,
        }:
            raise RunAlreadyTerminalError(historical)
        raise RunNotLiveError(run_id)
    return RunStatusResponse(
        run_id=snapshot.run_id,
        status=snapshot.status,
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
        persistence=await coordinator.get_persistence_info(run_id),
    )


@router.post(
    "/{run_id}/persistence/retry",
    response_model=PersistenceRetryResponse,
    status_code=202,
    responses={
        **error_responses(404, 409),
        200: {
            "model": PersistenceRetryResponse,
            "headers": REQUEST_ID_HEADERS,
        },
        202: {"headers": REQUEST_ID_HEADERS},
    },
)
async def retry_persistence(
    request: Request,
    run_id: UUID,
    response: Response,
) -> PersistenceRetryResponse:
    _reject_unknown_query_parameters(request, set())
    request.state.run_id = run_id
    coordinator = request.app.state.run_coordinator
    live = await coordinator.get_live_read(run_id)
    if live is None:
        await request.app.state.transcript_reader.get_historical_status(run_id)
        raise PersistenceRetryUnavailableError(run_id)
    persistence = await coordinator.retry_persistence(run_id)
    response.status_code = (
        200
        if persistence.state is PersistenceState.UP_TO_DATE
        else 202
    )
    return PersistenceRetryResponse(run_id=run_id, persistence=persistence)
