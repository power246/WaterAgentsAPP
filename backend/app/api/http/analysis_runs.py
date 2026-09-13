from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Request
from starlette.responses import Response

from app.api.schemas.analysis_runs import (
    AnalysisRunErrorResponse,
    AnalysisRunResponse,
    CreateAnalysisRunRequest,
)
from app.api.sse.response import create_event_response
from app.application.analysis_runs.models import AnalysisRunSnapshot

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
    responses={**error_responses(400), 201: {"headers": REQUEST_ID_HEADERS}},
)
async def create_run(request: Request, body: CreateAnalysisRunRequest) -> AnalysisRunSnapshot:
    request.state.log_private_query = body.query
    configuration = body.to_configuration_snapshot(request.app.state.settings.analysis_timezone)
    snapshot = await request.app.state.run_coordinator.create_run(body.query, configuration)
    request.state.run_id = snapshot.run_id
    return snapshot


@router.get(
    "/{run_id}/events", response_class=Response,
    description=(
        "Replays lifecycle events after Last-Event-ID, then streams new events and "
        "unnumbered comment keepalives. The stream closes after its terminal event; "
        "a consumed terminal cursor returns 204. Disconnecting does not cancel the run."
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
    run_id: UUID,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> Response:
    return await create_event_response(
        request.app.state.run_coordinator, run_id, last_event_id=last_event_id
    )


@router.post(
    "/{run_id}/cancel", response_model=AnalysisRunResponse,
    responses={**error_responses(404, 409), 200: {"headers": REQUEST_ID_HEADERS}},
)
async def cancel_run(request: Request, run_id: UUID) -> AnalysisRunSnapshot:
    return await request.app.state.run_coordinator.cancel_run(run_id)
