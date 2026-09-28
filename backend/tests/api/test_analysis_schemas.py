import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.api.schemas.analysis_runs import (
    AnalysisRunErrorResponse,
    AnalysisRunResponse,
    CreateAnalysisRunRequest,
    HttpPublicError,
    RunStatusResponse,
    ValidationViolation,
)
from app.application.analysis_runs.models import (
    AnalysisConfigurationSnapshot,
    AnalysisRunSnapshot,
    RunFailedEvent,
    RunStatus,
)
from app.infrastructure.config import AppSettings
from tests.m4_support import PARSER_MODEL, WATERBODY_MODEL, create_request

RUN_ID = "10ef14bd-d0e7-4a70-9728-1fbb4ca7a83c"
REQUEST_ID = "82ca317a-b20e-45da-9621-8c35a626cbba"
ERROR_ID = "4a1b720f-0eed-4be0-aaea-8d8300f439ca"
UTC_TIME = "2026-09-08T03:20:00Z"
ERROR = {
    "error_id": ERROR_ID,
    "request_id": REQUEST_ID,
    "run_id": RUN_ID,
    "code": "run_already_terminal",
    "message": "The run is already completed.",
    "failed_stage": "cancel_run",
    "retryable": False,
}


@pytest.mark.parametrize("input_mode", ["python", "json"])
@pytest.mark.parametrize("resolution", [None, "auto", "6h", "12h", "24h"])
def test_request_preserves_explicit_resolution_through_configuration_and_json(
    input_mode: str, resolution: str | None
) -> None:
    data = create_request("  Analyze water quality\nnear Austin  ")
    if resolution is not None:
        data["time_resolution"] = resolution
    request = (
        CreateAnalysisRunRequest.model_validate(data)
        if input_mode == "python"
        else CreateAnalysisRunRequest.model_validate_json(json.dumps(data))
    )
    configuration = request.to_configuration_snapshot("Asia/Shanghai")

    assert request.query == "Analyze water quality\nnear Austin"
    assert configuration.model_dump(mode="json") == {
        "analysis_timezone": "Asia/Shanghai",
        "time_resolution_preference": resolution or "12h",
        "time_resolution_preference_source": (
            "default" if resolution is None else "frontend"
        ),
        "parser_model": PARSER_MODEL.model_dump(mode="json"),
        "waterbody_model": WATERBODY_MODEL.model_dump(mode="json"),
        "memory_mode": "stateless",
    }
    assert ("time_resolution" in request.model_fields_set) is (resolution is not None)
    restored = CreateAnalysisRunRequest.model_validate_json(
        request.model_dump_json(exclude_unset=True)
    )
    assert restored.model_fields_set == request.model_fields_set
    assert restored.to_configuration_snapshot("Asia/Shanghai") == configuration
    assert AnalysisConfigurationSnapshot.model_validate_json(
        configuration.model_dump_json()
    ) == configuration


def test_configuration_does_not_follow_later_settings_changes() -> None:
    settings = AppSettings(analysis_timezone="America/Chicago")
    request = CreateAnalysisRunRequest.model_validate(
        create_request("Analyze water quality")
    )
    snapshot = request.to_configuration_snapshot(settings.analysis_timezone)

    settings.analysis_timezone = "Asia/Shanghai"

    assert snapshot.analysis_timezone == "America/Chicago"
    assert request.to_configuration_snapshot(settings.analysis_timezone).analysis_timezone == (
        "Asia/Shanghai"
    )
    with pytest.raises(ValidationError):
        request.to_configuration_snapshot("Not/A-Timezone")


@pytest.mark.parametrize("query", ["", " \n\t ", None, 12, True, [], {}])
def test_create_rejects_empty_and_non_string_query(query) -> None:
    body = create_request()
    body["query"] = query
    with pytest.raises(ValidationError):
        CreateAnalysisRunRequest.model_validate_json(json.dumps(body))


def test_create_requires_query() -> None:
    body = create_request()
    del body["query"]
    with pytest.raises(ValidationError, match="missing"):
        CreateAnalysisRunRequest.model_validate_json(json.dumps(body))


@pytest.mark.parametrize("resolution", [None, "1h", "12H", " 12h ", 12, True])
def test_create_rejects_explicit_null_and_invalid_resolution(resolution) -> None:
    with pytest.raises(ValidationError) as error:
        CreateAnalysisRunRequest.model_validate_json(
            json.dumps(
                create_request(
                    "Analyze water quality", time_resolution=resolution
                )
            )
        )
    assert error.value.errors()[0]["loc"] == ("time_resolution",)


@pytest.mark.parametrize("field", ["analysis_timezone", "fake_outcome", "api_key", "mode"])
def test_create_rejects_fields_outside_m2(field: str) -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        CreateAnalysisRunRequest.model_validate_json(
            json.dumps(
                create_request("Analyze water quality", **{field: "unexpected"})
            )
        )


@pytest.mark.parametrize("status", list(RunStatus))
def test_success_response_reuses_public_snapshot_without_query(status: RunStatus) -> None:
    request = CreateAnalysisRunRequest.model_validate(
        create_request("private query", time_resolution="6h")
    )
    snapshot = AnalysisRunSnapshot(
        run_id=UUID(RUN_ID),
        status=status,
        created_at=UTC_TIME,
        updated_at=UTC_TIME,
        configuration=request.to_configuration_snapshot("America/Chicago"),
    )
    response = AnalysisRunResponse.model_validate(snapshot, from_attributes=True)

    assert response.model_dump(mode="json") == {
        "run_id": RUN_ID,
        "status": status.value,
        "created_at": UTC_TIME,
        "updated_at": UTC_TIME,
        "configuration": {
            "analysis_timezone": "America/Chicago",
            "time_resolution_preference": "6h",
            "time_resolution_preference_source": "frontend",
            "parser_model": PARSER_MODEL.model_dump(mode="json"),
            "waterbody_model": WATERBODY_MODEL.model_dump(mode="json"),
            "memory_mode": "stateless",
        },
    }
    assert "private query" not in response.model_dump_json()
    assert AnalysisRunResponse.model_validate_json(response.model_dump_json()) == response
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AnalysisRunResponse.model_validate({**response.model_dump(), "query": request.query})


def test_http_error_defaults_and_optional_terminal_snapshot_round_trip() -> None:
    error = HttpPublicError.model_validate({**ERROR, "run_id": None})
    response = AnalysisRunErrorResponse(error=error)

    assert response.model_dump(mode="json") == {
        "error": {**ERROR, "run_id": None, "session_id": None},
        "run": None,
        "validation": [],
        "history_url": None,
    }
    snapshot = RunStatusResponse(
        run_id=UUID(RUN_ID),
        status=RunStatus.COMPLETED,
        created_at=UTC_TIME,
        updated_at=UTC_TIME,
        persistence=None,
    )
    response = AnalysisRunErrorResponse(error=HttpPublicError(**ERROR), run=snapshot)
    assert AnalysisRunErrorResponse.model_validate_json(response.model_dump_json()) == response
    assert response.run.run_id == response.error.run_id
    assert "private query" not in response.model_dump_json()


def test_http_error_requires_request_id_and_validates_uuid_fields() -> None:
    without_request_id = {key: value for key, value in ERROR.items() if key != "request_id"}
    with pytest.raises(ValidationError, match="missing"):
        HttpPublicError.model_validate(without_request_id)
    for field in ("request_id", "run_id", "error_id"):
        with pytest.raises(ValidationError):
            HttpPublicError.model_validate({**ERROR, field: "invalid-uuid"})


def test_validation_envelope_has_only_typed_safe_fields() -> None:
    violation = ValidationViolation(
        field=("body", "query"),
        type="string_too_short",
        message="The query must not be empty.",
    )
    response = AnalysisRunErrorResponse(
        error=HttpPublicError.model_validate({**ERROR, "code": "invalid_request"}),
        validation=(violation,),
    )

    assert response.model_dump(mode="json")["validation"] == [
        {
            "field": ["body", "query"],
            "type": "string_too_short",
            "message": "The query must not be empty.",
        }
    ]
    assert AnalysisRunErrorResponse.model_validate_json(response.model_dump_json()) == response
    nested = ValidationViolation(field=("body", "items", 0), type="missing", message="Required.")
    assert ValidationViolation.model_validate_json(nested.model_dump_json()).field[-1] == 0
    for field in ("input", "ctx", "details"):
        with pytest.raises(ValidationError, match="extra_forbidden"):
            ValidationViolation.model_validate({**violation.model_dump(), field: "private query"})


@pytest.mark.parametrize("path_item", [True, 1.5, None, {}])
def test_validation_path_rejects_non_string_non_integer_elements(path_item) -> None:
    with pytest.raises(ValidationError):
        ValidationViolation(field=("body", path_item), type="missing", message="Required.")


def test_error_envelopes_reject_arbitrary_details_and_tracebacks() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        HttpPublicError.model_validate({**ERROR, "traceback": "internal exception"})
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AnalysisRunErrorResponse.model_validate({"error": ERROR, "details": {"query": "raw"}})


def test_sse_failure_serializes_only_public_error_fields() -> None:
    http_error = HttpPublicError.model_validate(
        {**ERROR, "code": "workflow_failed", "failed_stage": "analysis_workflow"}
    )
    event = RunFailedEvent(
        run_id=UUID(RUN_ID),
        sequence=2,
        occurred_at=UTC_TIME,
        payload={"failed_at": UTC_TIME, "error": http_error},
    )

    assert set(event.model_dump(mode="json")["payload"]["error"]) == {
        "error_id", "code", "message", "failed_stage", "retryable"
    }


def test_request_json_schema_expresses_optional_non_nullable_resolution() -> None:
    schema = CreateAnalysisRunRequest.model_json_schema()

    assert schema["required"] == [
        "query", "parser_model", "waterbody_model", "memory_mode"
    ]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["query"]["minLength"] == 1
    assert schema["properties"]["time_resolution"]["default"] == "12h"
    assert schema["$defs"]["TimeResolutionPreference"]["enum"] == [
        "auto", "6h", "12h", "24h"
    ]
    assert "anyOf" not in schema["properties"]["time_resolution"]
