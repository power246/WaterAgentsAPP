import json

import pytest

from app.application.run_summary.errors import RunSummaryValidationError
from app.application.run_summary.validator import RunSummaryValidator
from tests.application.run_summary.support import (
    MODEL,
    NOW,
    TARGET_SOURCE,
    TOPOLOGY_SOURCE,
    draft_payload,
    summary_input,
)


def validate(payload, *, value=None):
    return RunSummaryValidator().validate(
        value or summary_input(),
        json.dumps(payload),
        model=MODEL,
        usage=None,
        generated_at=NOW,
    )


def test_valid_plain_language_numeric_highlight_preserves_exact_fact() -> None:
    _, result = validate(draft_payload())
    assert result.plain_language_summary.startswith("COMID 300")
    assert result.highlights[0].display_value == "7"
    assert result.highlights[0].display_unit == "mg/L"
    assert result.highlights[0].window_id == "w1"
    assert result.highlights[0].source_ref == TARGET_SOURCE


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"display_value": "8"}, "numeric_fact_mismatch"),
        ({"display_unit": "ug/L"}, "numeric_fact_mismatch"),
        ({"window_id": "w2"}, "invalid_output"),
    ],
)
def test_numeric_highlight_requires_exact_value_unit_window_and_ref(change, code) -> None:
    payload = draft_payload()
    payload["highlights"][0].update(change)
    with pytest.raises(RunSummaryValidationError) as captured:
        validate(payload)
    assert code in {item[0] for item in captured.value.issues}


def test_validator_rejects_bad_ref_url_unknown_comid_and_missing_scope_fields() -> None:
    bad_ref = TARGET_SOURCE.model_copy(update={"item_id": TOPOLOGY_SOURCE.item_id})
    cases = (
        draft_payload(source_ref=bad_ref),
        draft_payload(summary="See https://agent.test for COMID 300."),
        draft_payload(summary="COMID 999 has partial coverage."),
    )
    for payload in cases:
        with pytest.raises(RunSummaryValidationError):
            validate(payload)

    missing = draft_payload()
    del missing["target_comid"]
    del missing["graph_coverage"]
    with pytest.raises(RunSummaryValidationError):
        validate(missing)


def test_non_numeric_special_finding_is_allowed_with_an_input_ref() -> None:
    payload = draft_payload(
        highlights=[
            {
                "kind": "topology_limit",
                "text": "The upstream topology coverage is partial.",
                "source_ref": TOPOLOGY_SOURCE.model_dump(mode="json"),
                "display_value": None,
                "display_unit": None,
                "window_id": None,
            }
        ]
    )
    _, result = validate(payload)
    assert result.highlights[0].kind == "topology_limit"
