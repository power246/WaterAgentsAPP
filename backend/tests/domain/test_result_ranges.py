from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.application.reasoning.draft_models import ReasoningCellDraft
from app.domain import (
    HistoricalResultCellProjection,
    InferenceResult,
    OutcomeStatus,
    TimeWindowResult,
)
from tests.domain.test_results import local_day, make_result, make_series


def _window_payload():
    return {
        "cell_id": "cell:usgs:00400:w000",
        "start_time": "2026-10-16T00:00:00Z",
        "end_time": "2026-10-16T06:00:00Z",
        "outcome_status": "range_estimate",
        "representative_value": None,
        "value_lower": "6.50",
        "value_upper": "8.50",
        "unit_id": "standard units",
        "source": "inferred",
        "confidence": 0.15,
        "confidence_rationale": "Related water temperature informs a low-basis range.",
        "aggregation_method": None,
        "evidence_refs": ["e:related-temperature"],
        "assessment": {
            "summary": "Low-basis range prediction without a measured pH anchor.",
            "trend": None,
            "risk_level": "indeterminate",
            "risk_rationale": "The range does not establish a definite risk.",
            "reason_codes": [],
            "supporting_refs": ["e:related-temperature"],
        },
    }


def _draft_payload():
    payload = _window_payload()
    assessment = payload.pop("assessment")
    payload.pop("start_time")
    payload.pop("end_time")
    payload["target_unit_id"] = payload.pop("unit_id")
    payload.update(
        trend=assessment["trend"],
        risk_level=assessment["risk_level"],
        risk_rationale=assessment["risk_rationale"],
        reason_codes=assessment["reason_codes"],
        explanation=assessment["summary"],
        series_refs=[],
        quality_flags=[],
    )
    return payload


def _historical_payload():
    payload = _window_payload()
    assessment = payload.pop("assessment")
    payload["time_range"] = {
        "start": payload.pop("start_time"), "end": payload.pop("end_time"),
    }
    for key in ("cell_id", "evidence_refs", "aggregation_method", "confidence_rationale"):
        payload.pop(key)
    payload.update(
        parameter={"code_system": "usgs", "code": "00400"},
        window_id="w000", trend=assessment["trend"],
        risk_level=assessment["risk_level"],
    )
    return payload


@pytest.mark.parametrize(
    ("model", "build_payload"),
    [
        (TimeWindowResult, _window_payload),
        (ReasoningCellDraft, _draft_payload),
        (HistoricalResultCellProjection, _historical_payload),
    ],
)
def test_range_only_result_survives_json_and_preserves_low_confidence(model, build_payload):
    result = model.model_validate(build_payload())
    assert result.representative_value is None
    assert result.value_lower == "6.5"
    assert result.value_upper == "8.5"
    assert result.confidence == 0.15
    assert model.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "updates",
    [
        {"value_lower": None},
        {"value_upper": "6.5"},
        {"value_upper": "5"},
        {"representative_value": "7"},
        {"value_lower": 6.5},
        {"source": "observed"},
    ],
)
@pytest.mark.parametrize(
    ("model", "build_payload"),
    [
        (TimeWindowResult, _window_payload),
        (ReasoningCellDraft, _draft_payload),
        (HistoricalResultCellProjection, _historical_payload),
    ],
)
def test_range_outcome_rejects_invalid_shapes(model, build_payload, updates):
    payload = build_payload()
    payload.update(updates)
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_point_estimate_optional_range_must_contain_the_point():
    payload = _window_payload()
    payload.update(outcome_status="quantified", representative_value="7.2")
    result = TimeWindowResult.model_validate(payload)
    assert result.representative_value == "7.2"
    invalid = deepcopy(payload)
    invalid["representative_value"] = "9"
    with pytest.raises(ValidationError, match="within the value range"):
        TimeWindowResult.model_validate(invalid)


def test_range_result_cannot_bypass_the_series_unit_contract():
    result = make_result(
        series=(make_series(
            local_day(),
            point_statuses=(OutcomeStatus.RANGE_ESTIMATE, OutcomeStatus.RANGE_ESTIMATE),
        ),),
    )
    payload = result.model_dump(mode="json")
    assert InferenceResult.model_validate(payload) == result
    payload["series"][0]["points"][0]["unit_id"] = "unrelated unit"
    with pytest.raises(ValidationError, match="canonical_unit"):
        InferenceResult.model_validate(payload)


@pytest.mark.parametrize(
    ("model", "build_payload"),
    [
        (TimeWindowResult, _window_payload),
        (HistoricalResultCellProjection, _historical_payload),
    ],
)
def test_model_estimation_metadata_cannot_create_unmeasured_point(model, build_payload):
    payload = build_payload()
    payload.update(inference_method="model_estimate", inference_basis="related_evidence")
    assert model.model_validate(payload).representative_value is None
    payload.update(outcome_status="quantified", representative_value="7")
    with pytest.raises(ValidationError, match="original measured anchors"):
        model.model_validate(payload)


@pytest.mark.parametrize(
    "updates",
    [
        {"value_lower": None, "value_upper": None, "outcome_status": "quantified", "representative_value": "7"},
        {"inference_basis": None},
        {"inference_basis": "same_parameter_measurement"},
        {"source": "observed"},
    ],
)
def test_model_estimate_requires_range_basis_and_consistent_measured_status(updates):
    payload = _window_payload()
    payload.update(inference_method="model_estimate", inference_basis="general_knowledge")
    payload.update(updates)
    with pytest.raises(ValidationError):
        TimeWindowResult.model_validate(payload)
