from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Request
from starlette.responses import Response

from app.api.schemas.analysis_sessions import (
    AnalysisSessionErrorResponse,
    AnalysisSessionResponse,
    CreateAnalysisSessionRequest,
)
from app.api.sse.response import create_event_response
from app.application.analysis_sessions.models import AnalysisSessionSnapshot

REQUEST_ID_HEADERS = {
    "X-Request-ID": {
        "description": "Server-generated request UUID.",
        "schema": {"type": "string", "format": "uuid"},
    }
}


def error_responses(*statuses: int) -> dict[int, dict[str, object]]:
    return {
        status: {"model": AnalysisSessionErrorResponse, "headers": REQUEST_ID_HEADERS}
        for status in statuses
    }


router = APIRouter(
    prefix="/api/analysis/sessions",
    tags=["analysis-sessions"],
    responses=error_responses(405, 422, 500, 503),
)


@router.post(
    "", status_code=201, response_model=AnalysisSessionResponse,
    responses={**error_responses(400), 201: {"headers": REQUEST_ID_HEADERS}},
)
async def create_session(request: Request, body: CreateAnalysisSessionRequest) -> AnalysisSessionSnapshot:
    request.state.log_private_query = body.query
    configuration = body.to_configuration_snapshot(request.app.state.settings.analysis_timezone)
    snapshot = await request.app.state.session_coordinator.create_session(body.query, configuration)
    request.state.session_id = snapshot.session_id
    return snapshot


@router.get(
    "/{session_id}/events", response_class=Response,
    description=(
        "Replays lifecycle events after Last-Event-ID, then streams new events and "
        "unnumbered comment keepalives. The stream closes after its terminal event; "
        "a consumed terminal cursor returns 204. Disconnecting does not cancel the session. "
        "See docs/M2/m2_analysis_session_sse_api.md for frames and reconnection semantics."
    ),
    responses={
        **error_responses(400, 404, 409),
        200: {
            "description": "Lifecycle SSE stream with comment keepalive.",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
            "headers": REQUEST_ID_HEADERS,
        },
        204: {"description": "The terminal event has already been consumed.", "headers": REQUEST_ID_HEADERS},
    },
)
async def subscribe_events(
    request: Request,
    session_id: UUID,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> Response:
    return await create_event_response(
        request.app.state.session_coordinator, session_id, last_event_id=last_event_id
    )


@router.post(
    "/{session_id}/cancel", response_model=AnalysisSessionResponse,
    responses={**error_responses(404, 409), 200: {"headers": REQUEST_ID_HEADERS}},
)
async def cancel_session(request: Request, session_id: UUID) -> AnalysisSessionSnapshot:
    return await request.app.state.session_coordinator.cancel_session(session_id)
