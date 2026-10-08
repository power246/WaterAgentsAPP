import json
from datetime import datetime, timezone
from itertools import permutations
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from app.application.analysis_runs.models import (
    AnalysisConfigurationSnapshot,
    AnalysisRunSnapshot,
    CorrelationContext,
    LifecycleEventType,
    PublicError,
    RunCancelledEvent,
    RunCompletedEvent,
    RunCreatedEvent,
    RunFailedEvent,
    RunLifecycleEvent,
    RunStatus,
)
from tests.m4_support import PARSER_MODEL, WATERBODY_MODEL

RUN_ID = "10ef14bd-d0e7-4a70-9728-1fbb4ca7a83c"
ERROR_ID = "0463cd90-6d0f-47e9-9656-b4aa86352c61"
LOCAL_TIME = "2026-09-08T08:20:00-05:00"
UTC_TIME = "2026-09-08T13:20:00Z"
CONFIGURATION = {
    "analysis_timezone": "America/Chicago",
    "time_resolution_preference": "12h",
    "time_resolution_preference_source": "default",
    "parser_model": PARSER_MODEL.model_dump(mode="json"),
    "waterbody_model": WATERBODY_MODEL.model_dump(mode="json"),
    "memory_mode": "stateless",
    "unit_policy_version": 2,
    "unit_registry_digest": "a" * 64,
}
ERROR = {
    "error_id": ERROR_ID,
    "code": "workflow_failed",
    "message": "The analysis workflow failed.",
    "failed_stage": "analysis_workflow",
    "retryable": False,
}
SNAPSHOT = {
    "run_id": RUN_ID,
    "status": "pending",
    "created_at": LOCAL_TIME,
    "updated_at": LOCAL_TIME,
    "configuration": CONFIGURATION,
}
EVENT_CASES = (
    (
        RunCreatedEvent,
        "run_created",
        {
            "status": "pending",
            "created_at": LOCAL_TIME,
            "configuration": CONFIGURATION,
        },
    ),
    (
        RunCompletedEvent,
        "run_completed",
        {
            "status": "completed",
            "completed_at": LOCAL_TIME,
            "warning": None,
            "result_ref": None,
            "result_summary": None,
        },
    ),
    (
        RunFailedEvent,
        "run_failed",
        {"status": "failed", "failed_at": LOCAL_TIME, "error": ERROR},
    ),
    (
        RunCancelledEvent,
        "run_cancelled",
        {"status": "cancelled", "cancelled_at": LOCAL_TIME},
    ),
)
EVENT_ADAPTER = TypeAdapter(RunLifecycleEvent)


def event_data(event_type: str, payload: dict) -> dict:
    return {
        "schema_version": 2,
        "run_id": RUN_ID,
        "session_id": None,
        "sequence": 1 if event_type == "run_created" else 2,
        "type": event_type,
        "occurred_at": LOCAL_TIME,
        "payload": payload,
    }


def test_run_and_event_names_are_stable() -> None:
    assert [status.value for status in RunStatus] == [
        "pending",
        "running",
        "completed",
        "failed",
        "cancelled",
        "completed_with_persistence_error",
    ]
    assert [event.value for event in LifecycleEventType] == [
        "run_created",
        "run_completed",
        "run_failed",
        "run_cancelled",
        "session_created",
        "session_completed",
        "session_failed",
        "session_cancelled",
    ]


def test_snapshot_normalizes_uuid_and_utc_and_round_trips() -> None:
    snapshot = AnalysisRunSnapshot.model_validate(
        {**SNAPSHOT, "run_id": RUN_ID.upper()}
    )

    assert snapshot.run_id == UUID(RUN_ID)
    assert snapshot.created_at.tzinfo is timezone.utc
    assert snapshot.updated_at.tzinfo is timezone.utc
    assert snapshot.model_dump(mode="json") == {
        **SNAPSHOT,
        "created_at": UTC_TIME,
        "updated_at": UTC_TIME,
    }
    assert AnalysisRunSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot


def test_old_run_configuration_projects_explicit_legacy_identity() -> None:
    legacy = {key: value for key, value in CONFIGURATION.items() if not key.startswith("unit_")}
    snapshot = AnalysisRunSnapshot.model_validate(
        {**SNAPSHOT, "configuration": legacy}
    )
    assert snapshot.configuration.unit_policy_version == "legacy"
    assert snapshot.configuration.unit_registry_digest == "unknown"
    assert snapshot.configuration.analysis_timezone == "America/Chicago"
    assert AnalysisRunSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot

    with pytest.raises(ValidationError):
        AnalysisConfigurationSnapshot.model_validate(
            {**legacy, "unit_policy_version": 2}
        )


def test_snapshot_and_nested_configuration_are_immutable() -> None:
    data = {**SNAPSHOT, "configuration": dict(CONFIGURATION)}
    snapshot = AnalysisRunSnapshot.model_validate(data)
    data["configuration"]["analysis_timezone"] = "Asia/Shanghai"

    assert snapshot.configuration.analysis_timezone == "America/Chicago"
    with pytest.raises(ValidationError, match="frozen"):
        snapshot.status = RunStatus.COMPLETED
    with pytest.raises(ValidationError, match="frozen"):
        snapshot.configuration.analysis_timezone = "Asia/Shanghai"


@pytest.mark.parametrize("zone", ["", "  ", "Not/A-Timezone", "/etc/passwd", None])
def test_configuration_rejects_invalid_timezone(zone) -> None:
    with pytest.raises(ValidationError):
        AnalysisConfigurationSnapshot.model_validate(
            {**CONFIGURATION, "analysis_timezone": zone}
        )


@pytest.mark.parametrize("field", ["time_resolution", "time_resolution_source"])
def test_configuration_rejects_unknown_resolution_and_source(field: str) -> None:
    with pytest.raises(ValidationError):
        AnalysisConfigurationSnapshot.model_validate({**CONFIGURATION, field: "unknown"})


@pytest.mark.parametrize("event_class,event_type,payload", EVENT_CASES)
def test_typed_events_normalize_utc_and_round_trip(event_class, event_type, payload) -> None:
    event = event_class.model_validate(event_data(event_type, payload))
    serialized = json.loads(event.model_dump_json())
    timestamp_field = next(key for key in payload if key.endswith("_at"))

    assert serialized == {
        **event_data(event_type, payload),
        "occurred_at": UTC_TIME,
        "payload": {**payload, timestamp_field: UTC_TIME},
    }
    assert event.occurred_at.tzinfo is timezone.utc
    assert getattr(event.payload, timestamp_field).tzinfo is timezone.utc
    assert EVENT_ADAPTER.validate_json(event.model_dump_json()) == event
    assert type(EVENT_ADAPTER.validate_python(event.model_dump())) is event_class
    with pytest.raises(ValidationError, match="frozen"):
        event.sequence = 99
    with pytest.raises(ValidationError, match="frozen"):
        event.payload.status = "running"


@pytest.mark.parametrize("expected,other", list(permutations(EVENT_CASES, 2)))
def test_event_type_rejects_other_payloads_in_models_python_and_json(expected, other) -> None:
    event_class, event_type, _ = expected
    other_class, other_type, other_payload = other
    data = event_data(event_type, other_payload)

    with pytest.raises(ValidationError):
        EVENT_ADAPTER.validate_python(data)
    with pytest.raises(ValidationError):
        EVENT_ADAPTER.validate_json(json.dumps(data))
    with pytest.raises(ValidationError):
        event_class.model_validate(
            {
                **data,
                "payload": other_class.model_validate(
                    event_data(other_type, other_payload)
                ).payload,
            }
        )


@pytest.mark.parametrize("event_type", [None, "session_started", "unknown"])
def test_event_discriminator_is_required_and_rejects_unknown_types(event_type) -> None:
    data = event_data("run_completed", EVENT_CASES[1][2])
    if event_type is None:
        del data["type"]
    else:
        data["type"] = event_type
    with pytest.raises(ValidationError):
        EVENT_ADAPTER.validate_json(json.dumps(data))


def test_event_json_schema_preserves_type_payload_relationship() -> None:
    schema = EVENT_ADAPTER.json_schema()

    assert schema["discriminator"]["propertyName"] == "type"
    assert len(schema["oneOf"]) == 4
    for event_class, event_type, payload in EVENT_CASES:
        reference = schema["discriminator"]["mapping"][event_type]
        event_schema = schema["$defs"][reference.rsplit("/", 1)[-1]]
        assert event_schema["properties"]["type"]["const"] == event_type
        payload_name = event_schema["properties"]["payload"]["$ref"].rsplit("/", 1)[-1]
        payload_schema = schema["$defs"][payload_name]
        status_schema = payload_schema["properties"]["status"]
        if "const" in status_schema:
            assert status_schema["const"] == payload["status"]
        else:
            assert payload["status"] in status_schema["enum"]
        assert event_schema["additionalProperties"] is False
        assert payload_schema["additionalProperties"] is False


@pytest.mark.parametrize("sequence", [0, -1, True, False, 1.0, 1.5, "2", None])
def test_sequence_is_a_strict_positive_integer(sequence) -> None:
    data = {**event_data("run_completed", EVENT_CASES[1][2]), "sequence": sequence}

    with pytest.raises(ValidationError):
        EVENT_ADAPTER.validate_python(data)
    with pytest.raises(ValidationError):
        EVENT_ADAPTER.validate_json(json.dumps(data))
    if sequence is not None:
        with pytest.raises(ValidationError):
            CorrelationContext(event_sequence=sequence)


def test_created_sequence_is_one() -> None:
    with pytest.raises(ValidationError):
        RunCreatedEvent.model_validate(
            {**event_data("run_created", EVENT_CASES[0][2]), "sequence": 2}
        )


@pytest.mark.parametrize("version", [0, 1, True, 1.0, "1", None])
def test_event_schema_version_is_integer_two(version) -> None:
    data = {
        **event_data("run_completed", EVENT_CASES[1][2]),
        "schema_version": version,
    }
    with pytest.raises(ValidationError):
        EVENT_ADAPTER.validate_json(json.dumps(data))


@pytest.mark.parametrize("naive", [datetime(2026, 9, 8), "2026-09-08T00:00:00"])
def test_all_snapshot_and_event_timestamps_reject_naive_time(naive) -> None:
    for field in ("created_at", "updated_at"):
        with pytest.raises(ValidationError, match="timezone"):
            AnalysisRunSnapshot.model_validate({**SNAPSHOT, field: naive})
    for event_class, event_type, payload in EVENT_CASES:
        data = event_data(event_type, payload)
        with pytest.raises(ValidationError, match="timezone"):
            event_class.model_validate({**data, "occurred_at": naive})
        field = next(key for key in payload if key.endswith("_at"))
        with pytest.raises(ValidationError, match="timezone"):
            event_class.model_validate({**data, "payload": {**payload, field: naive}})


@pytest.mark.parametrize("invalid", ["not-a-uuid", "", 1])
def test_uuid_fields_reject_invalid_ids(invalid) -> None:
    with pytest.raises(ValidationError):
        AnalysisRunSnapshot.model_validate({**SNAPSHOT, "run_id": invalid})
    with pytest.raises(ValidationError):
        PublicError.model_validate({**ERROR, "error_id": invalid})
    with pytest.raises(ValidationError):
        EVENT_ADAPTER.validate_python(
            {**event_data("run_completed", EVENT_CASES[1][2]), "run_id": invalid}
        )
    for field in ("request_id", "run_id", "error_id"):
        with pytest.raises(ValidationError):
            CorrelationContext.model_validate({field: invalid})


@pytest.mark.parametrize("code", ["", " ", "Bad_code", "bad-code", "code\n", "a__b"])
def test_error_codes_and_stages_are_stable_snake_case(code: str) -> None:
    for field in ("code", "failed_stage"):
        with pytest.raises(ValidationError):
            PublicError.model_validate({**ERROR, field: code})


@pytest.mark.parametrize("retryable", [0, 1, "false", "true", None])
def test_retryable_requires_a_boolean(retryable) -> None:
    with pytest.raises(ValidationError):
        PublicError.model_validate({**ERROR, "retryable": retryable})


def test_public_error_and_correlation_round_trip() -> None:
    error = PublicError.model_validate(ERROR)
    context = CorrelationContext(
        request_id=UUID(ERROR_ID),
        run_id=UUID(RUN_ID),
        error_id=error.error_id,
        event_sequence=2,
    )

    assert PublicError.model_validate_json(error.model_dump_json()) == error
    assert CorrelationContext.model_validate_json(context.model_dump_json()) == context
    assert CorrelationContext.model_validate_json("{}").event_sequence is None
    assert PublicError.model_validate({**ERROR, "failed_stage": None}).failed_stage is None
    with pytest.raises(ValidationError):
        PublicError.model_validate({**ERROR, "message": " \n "})


def test_models_reject_extra_fields_at_each_public_boundary() -> None:
    model_inputs = [
        (AnalysisConfigurationSnapshot, CONFIGURATION),
        (AnalysisRunSnapshot, SNAPSHOT),
        (PublicError, ERROR),
        (CorrelationContext, {}),
    ]
    for event_class, event_type, payload in EVENT_CASES:
        event = event_class.model_validate(event_data(event_type, payload))
        model_inputs.extend(
            [(event_class, event_data(event_type, payload)), (type(event.payload), payload)]
        )
    for model_class, data in model_inputs:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            model_class.model_validate_json(json.dumps({**data, "query": "private query"}))
