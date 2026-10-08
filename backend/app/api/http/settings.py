"""Settings transport; validation and mutation live in SettingsService."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response

from app.api.http.analysis_runs import (
    REQUEST_ID_HEADERS, _reject_unknown_query_parameters, error_responses,
)
from app.api.schemas.analysis_runs import AnalysisRunErrorResponse
from app.api.schemas.settings_admin import (
    ConfirmActionRequest, PreviewUnitPolicyRequest, ReplaceSettingsRequest,
    SettingsConflictResponse, SettingsResponse, UnitPolicyPreviewResponse,
    UnitPolicyResponse,
)
from app.application.analysis_runs.creation import InvalidIdempotencyKeyError
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


@router.post("/unit-policies/preview", response_model=UnitPolicyPreviewResponse,
             responses={200: {"headers": REQUEST_ID_HEADERS},
                        409: {"model": AnalysisRunErrorResponse,
                              "headers": REQUEST_ID_HEADERS}})
async def preview_unit_policy(
    request: Request, response: Response, body: PreviewUnitPolicyRequest,
) -> UnitPolicyPreviewResponse:
    _reject_unknown_query_parameters(request, set())
    response.headers["Cache-Control"] = "no-store"
    result = await request.app.state.unit_policy_service.preview(
        base_version=body.base_version, entries=body.entries,
    )
    return UnitPolicyPreviewResponse(
        **result.preview.model_dump(),
        affected_dimensions=result.affected_dimensions,
        registry_digest=result.preview.proposed.registry_digest,
        confirmation=result.confirmation,
    )


@router.post("/unit-policies", response_model=UnitPolicyResponse,
             status_code=201, responses={201: {"headers": REQUEST_ID_HEADERS},
                                         409: {"model": AnalysisRunErrorResponse,
                                               "headers": REQUEST_ID_HEADERS}})
async def confirm_unit_policy(
    request: Request, response: Response, body: ConfirmActionRequest,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> UnitPolicyResponse:
    _reject_unknown_query_parameters(request, set())
    if idempotency_key.version != 4:
        raise InvalidIdempotencyKeyError()
    response.headers["Cache-Control"] = "no-store"
    policy = await request.app.state.unit_policy_service.confirm(
            token=body.confirmation_token,
            phrase=body.confirmation_phrase,
            idempotency_key=idempotency_key,
            raw_body=await request.body(),
        )
    return UnitPolicyResponse(**policy.model_dump())
