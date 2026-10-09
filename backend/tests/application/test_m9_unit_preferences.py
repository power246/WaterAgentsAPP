"""Parser unit intent stays bounded and resolves only within sealed parameters."""

import pytest
from pydantic import ValidationError

from app.application.query_understanding.context_models import ParsedIntentV1
from app.application.query_understanding.unit_preferences import (
    ParsedUnitIntent, UnitPreferenceIssue, UnitPreferenceResolver,
)
from app.application.evidence_policy.unit_conversion import convert_value
from app.application.reasoning.errors import DraftValidationError
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.settings_admin import UnitPolicyEntry, UnitPolicyProjection
from app.infrastructure.config import load_parameter_capability_registry
from tests.application.reasoning.m7_support import batch_payload, m7_fixture


REGISTRY = load_parameter_capability_registry()
DISCHARGE = ParameterKey(code_system="usgs", code="00060")
TEMPERATURE = ParameterKey(code_system="usgs", code="00010")


def _policy():
    return UnitPolicyProjection(
        version=2, registry_digest=REGISTRY.manifest_digest,
        entries=(UnitPolicyEntry(
            parameter=DISCHARGE, target_unit_id="cubic_meter_per_second",
        ),),
    )


def test_session_override_precedes_global_and_is_parameter_scoped() -> None:
    resolver = UnitPreferenceResolver(REGISTRY)
    scoped = (DISCHARGE, TEMPERATURE)
    global_only = resolver.resolve(intents=(), scoped_parameters=scoped, policy=_policy())
    assert [(item.parameter, item.target_unit_id, item.source) for item in global_only] == [
        (DISCHARGE, "cubic_meter_per_second", "global_policy")
    ]
    overridden = resolver.resolve(
        intents=(ParsedUnitIntent(raw_unit_alias="FT3/S", parameter_reference="Discharge"),),
        scoped_parameters=scoped, policy=_policy(),
    )
    assert [(item.parameter, item.target_unit_id, item.source) for item in overridden] == [
        (DISCHARGE, "cubic_foot_per_second", "session_override")
    ]
    assert resolver.resolve(intents=(), scoped_parameters=scoped, policy=None) == ()


@pytest.mark.parametrize("intent,expected", (
    ({"raw_unit_alias": "missing"}, "unit_unknown"),
    ({"raw_unit_alias": "m3/s", "parameter_reference": "Temperature, water"},
     "unit_dimension_mismatch"),
    ({"raw_unit_alias": "m3/s", "parameter_reference": "Dissolved oxygen"},
     "unit_parameter_out_of_scope"),
    ({"raw_unit_alias": "ug/l"}, "unit_not_applicable"),
))
def test_unit_issues_are_stable_and_do_not_expand_scope(intent, expected) -> None:
    with pytest.raises(UnitPreferenceIssue) as raised:
        UnitPreferenceResolver(REGISTRY).resolve(
            intents=(ParsedUnitIntent(**intent),),
            scoped_parameters=(DISCHARGE, TEMPERATURE), policy=None,
        )
    assert raised.value.code == expected


def test_conflicting_unit_aliases_for_one_parameter_are_ambiguous() -> None:
    with pytest.raises(UnitPreferenceIssue) as raised:
        UnitPreferenceResolver(REGISTRY).resolve(
            intents=(
                ParsedUnitIntent(raw_unit_alias="ft3/s"),
                ParsedUnitIntent(raw_unit_alias="m3/s"),
            ),
            scoped_parameters=(DISCHARGE,), policy=None,
        )
    assert raised.value.code == "unit_ambiguous"


def test_parser_rejects_formula_duplicate_and_unbounded_intents() -> None:
    base = {"parameters": [{"concept": "Discharge", "rationale": "Requested",
                            "explicitly_named": True}]}
    invalid_intents = (
        [{"raw_unit_alias": "m3/s", "factor": 0.0283168}],
        [{"raw_unit_alias": "m3/s"}, {"raw_unit_alias": "m3/s"}],
        [{"raw_unit_alias": "x" * 65}],
        [{"raw_unit_alias": "m3/s"}] * 7,
    )
    for intents in invalid_intents:
        with pytest.raises(ValidationError):
            ParsedIntentV1.model_validate({**base, "unit_intents": intents})


def test_conversion_is_decimal_and_model_value_needs_a_sealed_fact() -> None:
    oxygen = REGISTRY.get(ParameterKey(code_system="usgs", code="00300"))
    assert oxygen is not None
    converted = convert_value(
        REGISTRY, oxygen, "7", "mg/l",
        target_unit_id="micrograms_per_liter",
    )
    assert converted.source_value == "7"
    assert converted.value == "7000"
    assert converted.registry_digest == REGISTRY.manifest_digest
    assert converted.formula_id == "unit:milligram_per_liter->micrograms_per_liter:v1"

    _, plan, catalog, validator = m7_fixture()
    batch = plan.batch_plan.batches[0]
    draft = batch_payload(batch, catalog)
    draft["cells"][0]["target_unit_id"] = "ug/l"
    draft["cells"][0]["representative_value"] = "7000"
    with pytest.raises(DraftValidationError) as raised:
        validator.validate_batch(batch, draft)
    assert {issue.code for issue in raised.value.issues} >= {
        "unit_not_allowed", "quantified_value_not_backed",
    }
