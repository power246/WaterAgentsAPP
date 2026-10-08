import pytest
from pydantic import ValidationError

from app.api.schemas.settings_admin import (
    AdminOperationSnapshot, KeyStatusItem, PutCredentialRequest,
    ReplaceSettingsRequest,
)
from app.infrastructure.config import AppSettings
from app.main import create_app


TARGET_ROUTES = (
    ("/api/settings", "get", 200),
    ("/api/settings", "put", 200),
    ("/api/settings/unit-policies/preview", "post", 200),
    ("/api/settings/unit-policies", "post", 201),
    ("/api/keys", "get", 200),
    ("/api/keys/{key_name}", "put", 200),
    ("/api/keys/{key_name}", "delete", 200),
    ("/api/keys/{key_name}/test", "post", 200),
    ("/api/admin/storage", "get", 200),
    ("/api/admin/rag", "get", 200),
    ("/api/admin/operations", "get", 200),
    ("/api/admin/operations/{operation_id}", "get", 200),
    ("/api/admin/history-cleanups/preview", "post", 200),
    ("/api/admin/history-cleanups", "post", 202),
    ("/api/admin/rag/profile-handoffs/preview", "post", 200),
    ("/api/admin/rag/profile-handoffs", "post", 202),
    ("/api/admin/rag/generations/{generation_id}/failed-jobs/retry-preview", "post", 200),
    ("/api/admin/rag/generations/{generation_id}/failed-jobs/retries", "post", 202),
    ("/api/admin/rag/generations/{generation_id}/deletion-preview", "post", 200),
    ("/api/admin/rag/generations/{generation_id}/deletions", "post", 202),
)


@pytest.fixture(scope="module")
def openapi():
    return create_app(AppSettings(_env_file=None)).openapi()


@pytest.mark.parametrize("path,method,status", TARGET_ROUTES)
@pytest.mark.xfail(strict=True, reason="M9 M3-M6/M8 register real routes and headers")
def test_target_route_method_status_and_request_id_header(openapi, path, method, status):
    operation = openapi["paths"][path][method]
    responses = operation["responses"]
    assert str(status) in responses
    assert "X-Request-ID" in responses[str(status)]["headers"]
    for error_status in (422, 500):
        assert "X-Request-ID" in responses[str(error_status)]["headers"]


def test_runtime_openapi_is_frontend_type_source_including_session_sse_union(openapi):
    source = openapi["x-m9-frontend-type-source"]
    assert source["openapi"] == "/openapi.json"
    schemas = openapi["components"]["schemas"]
    for reference in (*source["sse_event_payloads"].values(), source["session_envelope"]):
        assert reference.removeprefix("#/components/schemas/") in schemas
    assert set(source["sse_event_payloads"]) == {
        "run_snapshot", "transcript_item", "message_started",
        "message_delta", "message_finalized", "stream_end",
    }
    assert "anyOf" in schemas["SessionProcessEvent"]["properties"]["payload"]


def test_request_response_schema_separation_and_key_allowlist(openapi):
    schemas = openapi["components"]["schemas"]
    assert "value" in schemas[PutCredentialRequest.__name__]["properties"]
    assert "value" not in schemas[KeyStatusItem.__name__]["properties"]
    assert "safe_result" in schemas[AdminOperationSnapshot.__name__]["properties"]
    assert "defaults" in schemas[ReplaceSettingsRequest.__name__]["properties"]
    assert schemas["CredentialName"]["enum"] == ["openai", "google_geocoding"]
    assert schemas["AdminActionKind"]["enum"] == [
        "history_cleanup", "profile_handoff", "retry_failed_jobs", "generation_deletion",
    ]
    with pytest.raises(ValidationError):
        PutCredentialRequest.model_validate({"value": "not empty", "echo": True})
    secret = "test-credential-value-1234"
    request = PutCredentialRequest(value=secret)
    assert secret not in repr(request)
    assert secret not in request.model_dump_json()
    with pytest.raises(ValidationError):
        PutCredentialRequest.model_validate({"value": " "})


def test_m9_error_catalog_has_stable_codes_status_and_no_duplicate(openapi):
    catalog = openapi["x-m9-public-errors"]
    by_code = {item["code"]: item for item in catalog}
    assert len(by_code) == len(catalog)
    assert by_code["settings_revision_conflict"]["status"] == 409
    assert by_code["credential_not_allowed"]["status"] == 404
    assert by_code["credential_store_unavailable"] == {
        "code": "credential_store_unavailable", "status": 503, "retryable": True,
    }
    assert by_code["admin_preview_expired"]["status"] == 409
    assert by_code["history_cleanup_dependency_conflict"]["status"] == 409
    assert by_code["rag_generation_not_found"]["status"] == 404
    assert by_code["idempotency_resource_deleted"]["status"] == 410


@pytest.mark.xfail(strict=True, reason="M9 M6 adds tombstone 410 to existing create route")
def test_create_tombstone_410_has_safe_error_envelope(openapi):
    response = openapi["paths"]["/api/analysis/runs"]["post"]["responses"]["410"]
    assert "X-Request-ID" in response["headers"]
    assert response["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/AnalysisRunErrorResponse"
    }
