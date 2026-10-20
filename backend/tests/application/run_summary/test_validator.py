import json

import pytest
from app.application.canonical_json import canonical_json_digest
from app.application.run_summary.errors import RunSummaryValidationError
from app.application.run_summary.models import RunSummaryInput
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


def input_with_window_id(window_id: str) -> RunSummaryInput:
    original = summary_input().model_dump(mode="json", exclude={"input_digest"})
    values = json.loads(json.dumps(original).replace("w1", window_id))
    return RunSummaryInput.model_validate(
        {**values, "input_digest": canonical_json_digest(values)}
    )


def test_valid_plain_language_numeric_highlight_preserves_exact_fact() -> None:
    _, result = validate(draft_payload())
    assert result.plain_language_summary.startswith("COMID 300")
    assert result.highlights[0].display_value == "7"
    assert result.highlights[0].display_unit == "mg/L"
    assert result.highlights[0].window_id == "w1"
    assert result.highlights[0].source_ref == TARGET_SOURCE


def test_range_summary_preserves_bounds_and_rejects_invented_midpoint() -> None:
    values = summary_input().model_dump(mode="json", exclude={"input_digest"})
    finding = values["target_result_projection"]["notable_findings"][0]
    finding.update(kind="range_estimate", display_value=None, value_lower="6", value_upper="8")
    value = RunSummaryInput.model_validate({**values, "input_digest": canonical_json_digest(values)})
    payload = draft_payload()
    payload["highlights"][0].update(
        kind="range_estimate", display_value=None, value_lower="6", value_upper="8",
        text="COMID 300 has a low-basis estimated range from 6 to 8 mg/L in w1.",
    )
    _, result = validate(payload, value=value)
    assert result.highlights[0].display_value is None
    assert (result.highlights[0].value_lower, result.highlights[0].value_upper) == ("6", "8")
    payload["highlights"][0].update(kind="notable_value", display_value="7")
    with pytest.raises(RunSummaryValidationError) as captured:
        validate(payload, value=value)
    assert ("numeric_fact_mismatch", "$.highlights[0]") in captured.value.issues


@pytest.mark.parametrize(
    "summary",
    (
        "Target 300 has limited evidence; upstream waterbody 100 is unresolved.",
        "COMIDs 100 and 300 are in the active scope.",
    ),
)
def test_plain_summary_accepts_active_comids_without_repeated_labels(summary) -> None:
    _, result = validate(draft_payload(summary=summary))
    assert result.plain_language_summary == summary


def test_numeric_highlight_accepts_bare_active_comid_and_exact_value() -> None:
    payload = draft_payload()
    payload["highlights"][0]["text"] = "Waterbody 300 has the cited value 7 mg/L in w1."
    _, result = validate(payload)
    assert result.highlights[0].display_value == "7"


def test_summary_accepts_known_numbered_window_id_in_prose_and_highlight() -> None:
    value = input_with_window_id("window-000")
    source = next(
        reference
        for reference in value.allowed_source_refs
        if reference.window_id == "window-000"
    )
    payload = draft_payload(
        source_ref=source,
        summary="Target 300 has a supported result in window-000.",
    )
    payload["highlights"][0]["text"] = (
        "COMID 300 has the cited value 7 mg/L in window-000."
    )
    payload["highlights"][0]["window_id"] = "window-000"
    _, result = validate(payload, value=value)
    assert result.highlights[0].window_id == "window-000"


@pytest.mark.parametrize("window_id", ("window-300", "window-000x", "w2"))
def test_summary_rejects_unknown_window_ids_even_when_digits_match_comid(
    window_id,
) -> None:
    payload = draft_payload(summary=f"Target 300 has a result in {window_id}.")
    with pytest.raises(RunSummaryValidationError) as captured:
        validate(payload, value=input_with_window_id("window-000"))
    assert ("unsupported_numeric_assertion", "$.plain_language_summary") in (
        captured.value.issues
    )


def test_numeric_highlight_rejects_unknown_window_id_in_text() -> None:
    value = input_with_window_id("window-000")
    source = next(
        reference
        for reference in value.allowed_source_refs
        if reference.window_id == "window-000"
    )
    payload = draft_payload(source_ref=source)
    payload["highlights"][0]["window_id"] = "window-000"
    payload["highlights"][0]["text"] = (
        "COMID 300 has the cited value 7 mg/L in window-300."
    )
    with pytest.raises(RunSummaryValidationError) as captured:
        validate(payload, value=value)
    assert ("unsupported_numeric_assertion", "$.highlights[0].text") in (
        captured.value.issues
    )


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


def test_unknown_highlight_kind_is_rejected() -> None:
    payload = draft_payload()
    payload["highlights"][0]["kind"] = "limitation"
    with pytest.raises(RunSummaryValidationError) as captured:
        validate(payload)
    assert ("invalid_output", "$.highlights.0.kind") in captured.value.issues


@pytest.mark.parametrize(
    "summary",
    (
        "COMID 300 has a fabricated value of 2 mg/L.",
        "COMID 300 changed by 2e3 units.",
        "Waterbody 999 has partial coverage.",
        "Target waterbody 300 has a pH of 8.2.",
        "COMIDs 100 and 999 are in the active scope.",
    ),
)
def test_plain_summary_rejects_numeric_facts_even_when_digits_exist_in_input(
    summary,
) -> None:
    with pytest.raises(RunSummaryValidationError) as captured:
        validate(draft_payload(summary=summary))
    assert ("unsupported_numeric_assertion", "$.plain_language_summary") in (
        captured.value.issues
    )


def test_highlight_text_must_match_its_exact_display_value() -> None:
    payload = draft_payload()
    payload["highlights"][0]["text"] = "COMID 300 has the value 2 mg/L in w1."
    with pytest.raises(RunSummaryValidationError) as captured:
        validate(payload)
    assert ("unsupported_numeric_assertion", "$.highlights[0].text") in (
        captured.value.issues
    )


def test_non_numeric_highlight_cannot_smuggle_a_numeric_fact() -> None:
    payload = draft_payload(
        highlights=[
            {
                "kind": "topology_limit",
                "text": "COMID 300 has 2 unavailable upstream branches.",
                "source_ref": TOPOLOGY_SOURCE.model_dump(mode="json"),
                "display_value": None,
                "display_unit": None,
                "window_id": None,
            }
        ]
    )
    with pytest.raises(RunSummaryValidationError) as captured:
        validate(payload)
    assert ("unsupported_numeric_assertion", "$.highlights[0].text") in (
        captured.value.issues
    )
