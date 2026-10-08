"""Allowlisted credential administration; no raw secret is returned."""

from fastapi import APIRouter, Request, Response

from app.api.http.analysis_runs import (
    REQUEST_ID_HEADERS, _reject_unknown_query_parameters, error_responses,
)
from app.api.schemas.settings_admin import (
    CredentialTestResponse, KeyStatusItem, KeyStatusResponse,
    PutCredentialRequest,
)
from app.application.settings_admin.errors import CredentialNotAllowedError
from app.domain.settings_admin import CredentialName


router = APIRouter(
    prefix="/api/keys", tags=["keys"],
    responses=error_responses(403, 405, 422, 500, 503),
)


def _name(value: str) -> CredentialName:
    try:
        return CredentialName(value)
    except ValueError:
        raise CredentialNotAllowedError() from None


def _prepare(request: Request, response: Response) -> None:
    _reject_unknown_query_parameters(request, set())
    response.headers["Cache-Control"] = "no-store"


@router.get("", response_model=KeyStatusResponse, responses={
    200: {"headers": REQUEST_ID_HEADERS},
})
async def list_keys(request: Request, response: Response) -> KeyStatusResponse:
    _prepare(request, response)
    return KeyStatusResponse(keys=tuple(
        KeyStatusItem.model_validate(item.model_dump())
        for item in await request.app.state.credential_service.statuses()
    ))


@router.put("/{key_name}", response_model=KeyStatusItem, responses={
    200: {"headers": REQUEST_ID_HEADERS},
    **error_responses(404),
})
async def save_key(
    request: Request, response: Response, key_name: str, body: PutCredentialRequest,
) -> KeyStatusItem:
    _prepare(request, response)
    status = await request.app.state.credential_service.save(
        _name(key_name), body.value,
    )
    return KeyStatusItem.model_validate(status.model_dump())


@router.delete("/{key_name}", response_model=KeyStatusItem, responses={
    200: {"headers": REQUEST_ID_HEADERS},
    **error_responses(404),
})
async def delete_key(
    request: Request, response: Response, key_name: str,
) -> KeyStatusItem:
    _prepare(request, response)
    status = await request.app.state.credential_service.delete(_name(key_name))
    return KeyStatusItem.model_validate(status.model_dump())


@router.post("/{key_name}/test", response_model=CredentialTestResponse, responses={
    200: {"headers": REQUEST_ID_HEADERS},
    **error_responses(404, 409),
})
async def test_key(
    request: Request, response: Response, key_name: str,
) -> CredentialTestResponse:
    _prepare(request, response)
    result = await request.app.state.credential_service.test(_name(key_name))
    return CredentialTestResponse(
        key_name=result.key_name,
        status="succeeded" if result.succeeded else "failed",
        safe_error=result.safe_error,
        checked_at=result.checked_at,
    )
