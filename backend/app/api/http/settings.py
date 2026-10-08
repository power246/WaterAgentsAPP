"""Settings transport; validation and mutation live in SettingsService."""

from fastapi import APIRouter, Request, Response

from app.api.http.analysis_runs import (
    REQUEST_ID_HEADERS, _reject_unknown_query_parameters, error_responses,
)
from app.api.schemas.settings_admin import (
    ReplaceSettingsRequest, SettingsConflictResponse, SettingsResponse,
)
from app.domain.settings_admin import SettingsSnapshot


router = APIRouter(
    prefix="/api/settings", tags=["settings"],
    responses=error_responses(405, 422, 500, 503),
)


def _response(snapshot: SettingsSnapshot, catalog) -> SettingsResponse:
    return SettingsResponse(
        **snapshot.model_dump(mode="python"), catalog=catalog,
    )


@router.get("", response_model=SettingsResponse, responses={
    200: {"headers": REQUEST_ID_HEADERS},
})
async def get_settings(request: Request, response: Response) -> SettingsResponse:
    _reject_unknown_query_parameters(request, set())
    response.headers["Cache-Control"] = "no-store"
    service = request.app.state.settings_service
    return _response(await service.get_snapshot(), service.catalog)


@router.put("", response_model=SettingsResponse, responses={
    200: {"headers": REQUEST_ID_HEADERS},
    409: {"model": SettingsConflictResponse, "headers": REQUEST_ID_HEADERS},
})
async def replace_settings(
    request: Request, response: Response, body: ReplaceSettingsRequest,
) -> SettingsResponse:
    _reject_unknown_query_parameters(request, set())
    response.headers["Cache-Control"] = "no-store"
    service = request.app.state.settings_service
    snapshot = await service.replace(
        expected_revision=body.expected_revision, defaults=body.defaults,
    )
    return _response(snapshot, service.catalog)
