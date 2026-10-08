import asyncio
import json
from uuid import UUID

import pytest
from fastapi import FastAPI, Request

from app.api.http.errors import handle_error
from app.application.settings_admin.errors import (
    AdminPreviewExpiredError, CredentialNotAllowedError,
    CredentialStoreUnavailableError, IdempotencyResourceDeletedError,
    SettingsRevisionConflictError, UnitDimensionMismatchError,
)


@pytest.mark.parametrize("error_type,status,code", (
    (SettingsRevisionConflictError, 409, "settings_revision_conflict"),
    (CredentialNotAllowedError, 404, "credential_not_allowed"),
    (CredentialStoreUnavailableError, 503, "credential_store_unavailable"),
    (UnitDimensionMismatchError, 422, "unit_dimension_mismatch"),
    (AdminPreviewExpiredError, 409, "admin_preview_expired"),
    (IdempotencyResourceDeletedError, 410, "idempotency_resource_deleted"),
))
def test_m9_error_uses_current_safe_envelope_and_request_id(error_type, status, code):
    application = FastAPI()
    application.state.id_factory = lambda: UUID("10000000-0000-4000-8000-000000000001")
    request = Request({
        "type": "http", "method": "PUT", "path": "/api/settings",
        "headers": [], "app": application, "path_params": {},
    })
    request.state.request_id = UUID("20000000-0000-4000-8000-000000000001")
    private = "raw-provider-secret-fragment"
    response = asyncio.run(handle_error(request, error_type(private)))
    body = json.loads(response.body)
    assert response.status_code == status
    assert body["error"]["code"] == code
    assert response.headers["X-Request-ID"] == str(request.state.request_id)
    assert response.headers["Cache-Control"] == "no-store"
    assert private not in response.body.decode("utf-8")
    assert body["validation"] == []
