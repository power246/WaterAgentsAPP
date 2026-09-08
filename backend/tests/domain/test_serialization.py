import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.domain.common import DomainEnum, DomainModel, JsonValue, UtcDatetime
from app.domain.hydrology import ParameterKey


class PayloadModel(DomainModel):
    payload: JsonValue


class TimestampModel(DomainModel):
    timestamp: UtcDatetime


class SampleStatus(DomainEnum):
    READY = "ready"


class EnumModel(DomainModel):
    status: SampleStatus


def test_domain_model_is_frozen_and_forbids_extra_fields() -> None:
    key = ParameterKey(code_system="usgs", code="00400")

    with pytest.raises(ValidationError, match="Instance is frozen"):
        key.code = "00010"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ParameterKey.model_validate(
            {"code_system": "usgs", "code": "00400", "unexpected": True}
        )


def test_json_payload_is_defensively_copied_at_input_and_dump_boundaries() -> None:
    source = {"series": [{"value": 7.2}], "flags": ["estimated"]}
    model = PayloadModel(payload=source)

    source["series"][0]["value"] = 99
    source["flags"].append("anomalous")
    assert model.payload == {
        "series": [{"value": 7.2}],
        "flags": ["estimated"],
    }

    dumped = model.model_dump()
    dumped["payload"]["series"][0]["value"] = -1
    assert model.payload["series"][0]["value"] == 7.2


@pytest.mark.parametrize(
    "input_method",
    ("constructor", "model_validate", "model_validate_json"),
)
def test_json_payload_preserves_whitespace_in_keys_and_values(
    input_method: str,
) -> None:
    payload = {
        "x": 1,
        " x ": 2,
        "text": " value ",
        "nested": {"key": "plain", " key ": [" nested value "]},
    }
    builders = {
        "constructor": lambda: PayloadModel(payload=payload),
        "model_validate": lambda: PayloadModel.model_validate({"payload": payload}),
        "model_validate_json": lambda: PayloadModel.model_validate_json(
            json.dumps({"payload": payload})
        ),
    }

    model = builders[input_method]()

    assert model.payload == payload
    assert PayloadModel.model_validate_json(model.model_dump_json()) == model


def test_business_strings_remain_trimmed() -> None:
    key = ParameterKey(code_system=" usgs ", code=" 00400 ")

    assert key.code_system == "usgs"
    assert key.code == "00400"


@pytest.mark.parametrize(
    "payload",
    [
        {"unsupported": object()},
        {"tuple": (1, 2)},
        {"nan": float("nan")},
        {"infinity": float("inf")},
    ],
)
def test_json_payload_rejects_non_json_values(payload) -> None:
    with pytest.raises(ValidationError):
        PayloadModel(payload=payload)


@pytest.mark.parametrize("number", ("NaN", "Infinity", "-Infinity", "1e400"))
def test_json_payload_rejects_non_finite_numbers_from_json(number: str) -> None:
    raw = f'{{"payload":{{"nested":[{number}]}}}}'

    with pytest.raises(ValidationError, match="JSON numbers must be finite"):
        PayloadModel.model_validate_json(raw)


def test_json_payload_round_trips() -> None:
    model = PayloadModel(
        payload={
            "null": None,
            "boolean": True,
            "integer": 1,
            "number": 1.5,
            "text": "value",
            "array": [1, {"nested": "value"}],
        }
    )

    assert PayloadModel.model_validate_json(model.model_dump_json()) == model


def test_aware_datetime_is_normalized_to_utc_and_naive_is_rejected() -> None:
    source = datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=-6)))

    model = TimestampModel(timestamp=source)

    assert model.timestamp == datetime(2026, 1, 1, 18, tzinfo=timezone.utc)
    assert TimestampModel.model_validate_json(model.model_dump_json()) == model
    with pytest.raises(ValidationError, match="must include timezone information"):
        TimestampModel(timestamp=datetime(2026, 1, 1, 12))


def test_domain_enum_uses_lowercase_string_values_and_round_trips() -> None:
    model = EnumModel(status=SampleStatus.READY)

    assert model.model_dump_json() == '{"status":"ready"}'
    assert EnumModel.model_validate_json(model.model_dump_json()) == model

    with pytest.raises(ValueError, match="lowercase"):

        class InvalidStatus(DomainEnum):
            INVALID = "INVALID"

    with pytest.raises(ValueError, match="strings"):

        class InvalidType(DomainEnum):
            INVALID = 1
