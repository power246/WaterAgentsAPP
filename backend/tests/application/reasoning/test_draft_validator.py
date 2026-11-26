import asyncio
import copy
import json
from datetime import timedelta

import pytest

from app.application.reasoning import (
    DraftValidationError,
    DraftValidator,
    ReasoningCellDraft,
    ReasoningEvidenceCatalog,
    build_reasoning_evidence_catalog,
    seal_evidence_input,
)
from app.application.reasoning.result_assembler import _point_from_draft
from app.domain import (
    AggregationMethod,
    AttributionBasis,
    DataQualityFlag,
    EvidenceApplicability,
    EvidenceQualityClass,
    EvidenceResultEligibility,
    ParameterKey,
    SourceRecordReference,
)
from app.domain.evidence.measured_anchor import MeasuredAnchor
from tests.application.reasoning.m7_support import (
    batch_payload,
    m7_fixture,
    rebuild_catalog,
    synthesis_payload,
    validated_reasoning,
)
from tests.application.reasoning.test_task_and_skeleton import _validated_fixture


def _issue_codes(error: DraftValidationError) -> set[str]:
    return {issue.code for issue in error.issues}


def test_quantified_directional_and_agent_indeterminate_shapes_validate() -> None:
    _skeleton, plan, catalog, validator = m7_fixture()
    batch = plan.batch_plan.batches[0]

    quantified = validator.validate_batch(batch, batch_payload(batch, catalog))
    assert len(quantified.cells) == 4

    directional = batch_payload(batch, catalog)
    for cell in directional["cells"]:
        cell.update(
            outcome_status="directional",
            representative_value=None,
            target_unit_id=None,
            source="inferred",
            trend="rising",
            risk_level="medium",
            aggregation_method=None,
        )
    assert all(
        cell.outcome_status.value == "directional"
        for cell in validator.validate_batch(batch, directional).cells
    )

    indeterminate = batch_payload(batch, catalog)
    for cell in indeterminate["cells"]:
        cell.update(
            outcome_status="indeterminate",
            representative_value=None,
            target_unit_id=None,
            source=None,
            trend="uncertain",
            risk_level="indeterminate",
            confidence=0.0,
            aggregation_method=None,
            reason_codes=["insufficient_evidence"],
        )
    assert all(
        cell.outcome_status.value == "indeterminate"
        for cell in validator.validate_batch(batch, indeterminate).cells
    )


def test_batch_identity_cell_set_order_and_strict_scalar_failures() -> None:
    _skeleton, plan, catalog, validator = m7_fixture()
    batch = plan.batch_plan.batches[0]
    valid = batch_payload(batch, catalog)
    corruptions = []

    missing = copy.deepcopy(valid)
    missing["cells"].pop()
    corruptions.append(missing)
    duplicate = copy.deepcopy(valid)
    duplicate["cells"].append(copy.deepcopy(duplicate["cells"][0]))
    corruptions.append(duplicate)
    extra = copy.deepcopy(valid)
    extra_cell = copy.deepcopy(extra["cells"][0])
    extra_cell["cell_id"] = "cell:extra"
    extra["cells"].append(extra_cell)
    corruptions.append(extra)
    reversed_cells = copy.deepcopy(valid)
    reversed_cells["cells"].reverse()
    corruptions.append(reversed_cells)

    for field, value in (
        ("evidence_refs", ["e:unknown"]),
        ("series_refs", ["series:unknown"]),
        ("target_unit_id", "unknown-unit"),
        ("aggregation_method", "sum"),
        ("source", "unknown"),
        ("risk_level", "unknown"),
        ("confidence", True),
        ("representative_value", 7.1),
        ("risk_rationale", " "),
    ):
        item = copy.deepcopy(valid)
        item["cells"][0][field] = value
        corruptions.append(item)
    wrong_scope = copy.deepcopy(valid)
    wrong_scope["scope_digest"] = "sha256:wrong"
    corruptions.append(wrong_scope)
    extra_field = copy.deepcopy(valid)
    extra_field["cells"][0]["availability"] = "complete"
    corruptions.append(extra_field)

    for payload in corruptions:
        with pytest.raises(DraftValidationError):
            validator.validate_batch(batch, payload)

    for constant in (float("nan"), float("inf"), float("-inf")):
        payload = copy.deepcopy(valid)
        payload["cells"][0]["confidence"] = constant
        raw = json.dumps(payload, allow_nan=True)
        with pytest.raises(DraftValidationError) as raised:
            validator.validate_batch(batch, raw)
        assert "invalid_json" in _issue_codes(raised.value)


def test_only_indirect_is_directional_at_most_and_aggregate_requires_method() -> None:
    _skeleton, plan, catalog, _validator = m7_fixture()
    batch = plan.batch_plan.batches[0]
    first_ref = batch.cells[0].allowed_evidence_refs[0]
    facts = tuple(
        fact.model_copy(
            update={
                "applicability": EvidenceApplicability.ALLOWED_INDIRECT,
                "result_eligibility": EvidenceResultEligibility.INFERRED_ONLY,
            }
        )
        if fact.evidence_ref == first_ref
        else fact
        for fact in catalog.facts
    )
    scopes = tuple(
        scope.model_copy(
            update={
                "target_direct_refs": (),
                "target_indirect_refs": (first_ref,),
            }
        )
        if scope.cell_id == batch.cells[0].cell_id
        else scope
        for scope in catalog.cell_scopes
    )
    indirect_catalog = rebuild_catalog(catalog, facts=facts, cell_scopes=scopes)
    indirect_validator = DraftValidator(indirect_catalog)
    payload = batch_payload(batch, indirect_catalog)
    cell = payload["cells"][0]
    cell.update(
        outcome_status="directional",
        representative_value=None,
        target_unit_id=None,
        source="inferred",
        trend="rising",
        aggregation_method=None,
    )
    indirect_validator.validate_batch(batch, payload)

    quantified = copy.deepcopy(payload)
    quantified["cells"][0].update(
        outcome_status="quantified",
        representative_value="7",
        target_unit_id="mg/l",
        aggregation_method=None,
    )
    with pytest.raises(DraftValidationError) as raised:
        indirect_validator.validate_batch(batch, quantified)
    assert "only_indirect_quantified" in _issue_codes(raised.value)

    aggregate_facts = tuple(
        fact.model_copy(
            update={
                "payload_type": "aggregate",
                "result_eligibility": EvidenceResultEligibility.INFERRED_ONLY,
                "aggregation_method": AggregationMethod.MEAN,
                "source_record_refs": ("s:a", "s:b"),
            }
        )
        if fact.evidence_ref == first_ref
        else fact
        for fact in catalog.facts
    )
    aggregate_catalog = rebuild_catalog(catalog, facts=aggregate_facts)
    aggregate_validator = DraftValidator(aggregate_catalog)
    aggregate = batch_payload(batch, aggregate_catalog)
    aggregate["cells"][0].update(source="inferred", aggregation_method="mean")
    aggregate_validator.validate_batch(batch, aggregate)
    missing_method = copy.deepcopy(aggregate)
    missing_method["cells"][0]["aggregation_method"] = None
    with pytest.raises(DraftValidationError) as raised:
        aggregate_validator.validate_batch(batch, missing_method)
    assert "aggregation_mismatch" in _issue_codes(raised.value)


def test_materialized_relation_cites_its_own_ref_without_contextual_input() -> None:
    _skeleton, plan, catalog, _validator = m7_fixture(attribution_context=True)
    batch = plan.batch_plan.batches[0]
    first_cell = batch.cells[0]
    relation_ref = first_cell.allowed_evidence_refs[0]
    context_ref = "e:context"
    original = next(
        fact for fact in catalog.facts if fact.evidence_ref == relation_ref
    )
    relation = original.model_copy(update={
        "payload_type": "relation_support",
        "applicability": EvidenceApplicability.ALLOWED_INDIRECT,
        "result_eligibility": EvidenceResultEligibility.INFERRED_ONLY,
        "value": None,
        "unit_id": None,
        "source_record_refs": (),
        "dependency_evidence_refs": (context_ref,),
        "series_ref": None,
    })
    scopes = tuple(
        scope.model_copy(update={
            "target_direct_refs": (),
            "target_indirect_refs": (relation_ref,),
        }) if scope.cell_id == first_cell.cell_id else scope
        for scope in catalog.cell_scopes
    )
    facts = tuple(
        relation if fact.evidence_ref == relation_ref else fact
        for fact in catalog.facts
    )
    with pytest.raises(ValueError, match="dependency is unknown"):
        rebuild_catalog(
            catalog,
            facts=tuple(fact for fact in facts if fact.evidence_ref != context_ref),
            cell_scopes=scopes,
        )
    catalog = rebuild_catalog(catalog, facts=facts, cell_scopes=scopes)
    payload = batch_payload(batch, catalog)
    payload["cells"][0].update(
        outcome_status="directional",
        representative_value=None,
        target_unit_id=None,
        source="inferred",
        trend="rising",
        risk_level="low",
        confidence=0.3,
        aggregation_method=None,
        series_refs=[],
    )
    validated = DraftValidator(catalog).validate_batch(batch, payload)
    assert validated.cells[0].evidence_refs == (relation_ref,)
    assert context_ref not in validated.cells[0].evidence_refs

    out_of_scope = copy.deepcopy(payload)
    out_of_scope["cells"][0]["evidence_refs"].append(context_ref)
    with pytest.raises(DraftValidationError) as raised:
        DraftValidator(catalog).validate_batch(batch, out_of_scope)
    assert "evidence_out_of_scope" in _issue_codes(raised.value)


def test_materialized_relation_attribution_preserves_other_dependency_checks() -> None:
    skeleton, plan, catalog, _validator = m7_fixture(
        attribution_context=True, conversion_first=True
    )
    facts = tuple(
        fact.model_copy(update={"dependency_evidence_refs": ("e:context",)})
        if fact.evidence_ref == "e:relation" else fact
        for fact in catalog.facts
    )
    catalog = rebuild_catalog(catalog, facts=facts)
    validator = DraftValidator(catalog)
    reasoning = validated_reasoning(plan, validator, catalog)
    payload = synthesis_payload(skeleton, plan, reasoning)
    factor = payload["attribution_summary"]["primary_factors"][0]
    factor.update(basis="correlated", evidence_refs=["e:relation"])
    validator.validate_synthesis(
        skeleton, plan.batch_plan, plan.synthesis_plan, reasoning, payload
    )

    unclosed_conversion = copy.deepcopy(payload)
    factor = unclosed_conversion["attribution_summary"]["primary_factors"][0]
    factor.update(basis="inferred", evidence_refs=["e:conversion"])
    with pytest.raises(DraftValidationError) as raised:
        validator.validate_synthesis(
            skeleton, plan.batch_plan, plan.synthesis_plan,
            reasoning, unclosed_conversion,
        )
    assert "attribution_basis_invalid" in _issue_codes(raised.value)


def _numeric_derived_fixture():
    _skeleton, plan, catalog, _validator = m7_fixture()
    batch = plan.batch_plan.batches[0]
    input_ref = batch.cells[0].allowed_evidence_refs[0]
    result_ref = batch.cells[1].allowed_evidence_refs[0]
    input_fact = next(fact for fact in catalog.facts if fact.evidence_ref == input_ref)
    result_fact = next(fact for fact in catalog.facts if fact.evidence_ref == result_ref)
    derived = result_fact.model_copy(update={
        "payload_type": "numeric_derived",
        "applicability": EvidenceApplicability.ALLOWED_INDIRECT,
        "result_eligibility": EvidenceResultEligibility.INFERRED_ONLY,
        "source_record_refs": (),
        "dependency_evidence_refs": (input_ref,),
        "quality_flags": (DataQualityFlag.ESTIMATED,),
        "numeric_method_id": "test:registered_method",
        "numeric_method_version": 1,
        "numeric_task_id": "task:test:p0:w001",
        "numeric_plan_digest": "sha256:test-plan",
        "numeric_plan_item_id": "item:test-plan",
        "numeric_method_registry_digest": "sha256:test-registry",
        "numeric_confidence_cap": 0.6,
        "numeric_input_bindings": ((input_ref, input_fact.item_id, "w000"),),
    })
    scopes = tuple(
        scope.model_copy(update={
            "target_direct_refs": (),
            "target_indirect_refs": (result_ref,),
        }) if scope.cell_id == batch.cells[1].cell_id else scope
        for scope in catalog.cell_scopes
    )
    catalog = rebuild_catalog(
        catalog,
        facts=tuple(
            derived if fact.evidence_ref == result_ref else fact
            for fact in catalog.facts
        ),
        cell_scopes=scopes,
    )
    payload = batch_payload(batch, catalog)
    payload["cells"][1].update(
        source="inferred",
        confidence=0.6,
        quality_flags=["estimated"],
        aggregation_method=None,
    )
    return batch, catalog, payload, input_ref, result_ref


def _range_derived_fixture():
    batch, catalog, payload, _input_ref, result_ref = _numeric_derived_fixture()
    facts = tuple(
        fact.model_copy(update={
            "value": None,
            "value_lower": "5",
            "value_upper": "9",
            "method_kind": "model_estimate",
            "inference_basis": "general_knowledge",
            "dependency_evidence_refs": (),
            "numeric_input_bindings": (),
            "numeric_confidence_cap": 0.1,
        }) if fact.evidence_ref == result_ref else fact
        for fact in catalog.facts
    )
    catalog = rebuild_catalog(catalog, facts=facts)
    payload["cells"][1].update(
        outcome_status="range_estimate",
        representative_value=None,
        value_lower="5",
        value_upper="9",
        confidence=0.1,
    )
    return batch, catalog, payload, result_ref


def _mixed_model_estimate_fixture():
    _skeleton, plan, catalog, _validator = m7_fixture()
    batch = plan.batch_plan.batches[0]
    result_refs = tuple(cell.allowed_evidence_refs[0] for cell in batch.cells)
    by_ref = {fact.evidence_ref: fact for fact in catalog.facts}
    facts = []
    for index, ref in enumerate(result_refs):
        original = by_ref[ref]
        point = index != 1
        anchor = MeasuredAnchor(
            parameter=original.parameter_refs[0],
            source_record_refs=(SourceRecordReference(
                item_id="source-records", record_id=f"reading-{index}",
            ),),
            origin_comid="upstream",
            evidence_time_range=original.time_range,
            path_comids=("upstream", catalog.target_comid),
        )
        facts.append(original.model_copy(update={
            "payload_type": "numeric_derived",
            "applicability": EvidenceApplicability.ALLOWED_INDIRECT,
            "result_eligibility": EvidenceResultEligibility.INFERRED_ONLY,
            "value": "7" if point else None,
            "value_lower": "5",
            "value_upper": "9",
            "method_kind": "model_estimate",
            "inference_basis": (
                "same_parameter_measurement" if point else "general_knowledge"
            ),
            "measured_anchors": (anchor,) if point else (),
            "source_record_refs": (),
            "quality_flags": (DataQualityFlag.ESTIMATED,),
            "numeric_method_id": "test:model_estimate",
            "numeric_method_version": 1,
            "numeric_task_id": f"task:test:window-{index}",
            "numeric_plan_digest": "sha256:test-plan",
            "numeric_plan_item_id": f"item:test-plan-{index}",
            "numeric_method_registry_digest": "sha256:test-registry",
            "numeric_confidence_cap": 0.3,
        }))
    scopes = tuple(scope.model_copy(update={
        "target_direct_refs": (),
        "target_indirect_refs": (result_refs[index],),
    }) for index, scope in enumerate(catalog.cell_scopes))
    catalog = rebuild_catalog(catalog, facts=tuple(facts), cell_scopes=scopes)
    payload = batch_payload(batch, catalog)
    for index, cell in enumerate(payload["cells"]):
        cell.update(
            outcome_status="quantified" if index != 1 else "range_estimate",
            representative_value="7" if index != 1 else None,
            value_lower="5",
            value_upper="9",
            source="inferred",
            confidence=0.3,
            aggregation_method=None,
            quality_flags=["estimated"],
            explanation="The saved numeric estimate has a point and bounds."
            if index != 1 else "The saved numeric estimate has only a range.",
        )
    return batch, catalog, payload, result_refs


def test_missing_range_bounds_replay_from_unique_cited_sealed_fact(caplog) -> None:
    batch, catalog, payload, _result_ref = _range_derived_fixture()
    validator = DraftValidator(catalog)
    baseline = validator.validate_batch(batch, payload)
    raw_repair = copy.deepcopy(payload)
    del raw_repair["cells"][1]["value_lower"]
    del raw_repair["cells"][1]["value_upper"]
    encoded = json.dumps(raw_repair)

    with caplog.at_level("INFO", logger="wateragents.reasoning"):
        restored = validator.validate_batch(batch, encoded)

    assert restored.validation_digest == baseline.validation_digest
    assert restored.cells[1].value_lower == "5"
    assert restored.cells[1].value_upper == "9"
    assert "value_lower" not in raw_repair["cells"][1]
    assert "value_upper" not in raw_repair["cells"][1]
    assert validator.validate_batch(batch, encoded).validation_digest == restored.validation_digest
    records = [
        record for record in caplog.records
        if getattr(record, "event_name", None)
        == "reasoning_draft_numeric_range_filled"
    ]
    assert {record.schema_path for record in records} == {
        "$.cells[1].value_lower", "$.cells[1].value_upper",
    }
    assert all(record.schema_rule == "unique_cited_numeric_derived_range"
               for record in records)


@pytest.mark.parametrize("bounds_form", ("missing", "null"))
def test_quantified_point_bounds_replay_from_unique_cited_sealed_fact(
    bounds_form: str, caplog,
) -> None:
    batch, catalog, payload, _refs = _mixed_model_estimate_fixture()
    validator = DraftValidator(catalog)
    baseline = validator.validate_batch(batch, payload)
    raw = copy.deepcopy(payload)
    for index in (0, 2, 3):
        if bounds_form == "missing":
            del raw["cells"][index]["value_lower"]
            del raw["cells"][index]["value_upper"]
        else:
            raw["cells"][index]["value_lower"] = None
            raw["cells"][index]["value_upper"] = None
    encoded = json.dumps(raw)

    with caplog.at_level("INFO", logger="wateragents.reasoning"):
        restored = validator.validate_batch(batch, encoded)

    assert restored.validation_digest == baseline.validation_digest
    assert validator.validate_batch(batch, encoded).validation_digest == baseline.validation_digest
    replayed_catalog = ReasoningEvidenceCatalog.model_validate_json(
        catalog.model_dump_json()
    )
    assert replayed_catalog.catalog_digest == catalog.catalog_digest
    assert DraftValidator(replayed_catalog).validate_batch(
        batch, encoded
    ).validation_digest == baseline.validation_digest
    assert [cell.representative_value for cell in restored.cells] == [
        "7", None, "7", "7",
    ]
    assert all(
        (cell.value_lower, cell.value_upper) == ("5", "9")
        for cell in restored.cells
    )
    assert {record.schema_path for record in caplog.records if getattr(
        record, "event_name", None
    ) == "reasoning_draft_numeric_range_filled"} == {
        f"$.cells[{index}].{bound}"
        for index in (0, 2, 3)
        for bound in ("value_lower", "value_upper")
    }


@pytest.mark.parametrize("case", (
    "one_bound_missing", "one_bound_null", "changed_bound", "wrong_point",
    "wrong_unit", "out_of_scope_ref", "range_only_as_point", "wrong_outcome",
))
def test_quantified_missing_bounds_still_rejects_nonmatching_fact(case: str) -> None:
    batch, catalog, payload, refs = _mixed_model_estimate_fixture()
    cell = payload["cells"][0]
    if case == "one_bound_missing":
        del cell["value_lower"]
    elif case == "one_bound_null":
        cell["value_lower"] = None
    elif case == "changed_bound":
        cell["value_lower"] = "4"
    elif case == "wrong_point":
        cell["representative_value"] = "8"
        cell["value_lower"] = None
        cell["value_upper"] = None
    elif case == "wrong_unit":
        cell["target_unit_id"] = "wrong-unit"
        cell["value_lower"] = None
        cell["value_upper"] = None
    elif case == "out_of_scope_ref":
        cell["evidence_refs"].append(refs[1])
        cell["value_lower"] = None
        cell["value_upper"] = None
    elif case == "range_only_as_point":
        cell["evidence_refs"] = [refs[1]]
        cell["value_lower"] = None
        cell["value_upper"] = None
    else:
        cell["outcome_status"] = "directional"
        cell["value_lower"] = None
        cell["value_upper"] = None

    with pytest.raises(DraftValidationError):
        DraftValidator(catalog).validate_batch(batch, payload)


def test_multiple_cited_point_facts_do_not_fill_missing_bounds() -> None:
    batch, catalog, payload, refs = _mixed_model_estimate_fixture()
    first = next(fact for fact in catalog.facts if fact.evidence_ref == refs[0])
    second_ref = "e:second-point"
    second = first.model_copy(update={
        "evidence_ref": second_ref,
        "item_id": f"item:{second_ref}",
    })
    scopes = tuple(scope.model_copy(update={
        "target_indirect_refs": tuple(sorted((refs[0], second_ref))),
    }) if scope.cell_id == batch.cells[0].cell_id else scope
        for scope in catalog.cell_scopes)
    catalog = rebuild_catalog(
        catalog, facts=(*catalog.facts, second), cell_scopes=scopes,
    )
    planned = batch.cells[0].model_copy(update={
        "allowed_evidence_refs": tuple(sorted((refs[0], second_ref))),
    })
    batch = batch.model_copy(update={
        "cells": (planned, *batch.cells[1:]),
    })
    payload["cells"][0]["evidence_refs"] = [refs[0], second_ref]
    payload["cells"][0]["value_lower"] = None
    payload["cells"][0]["value_upper"] = None

    with pytest.raises(DraftValidationError):
        DraftValidator(catalog).validate_batch(batch, payload)


def test_point_fact_in_different_window_does_not_fill_missing_bounds() -> None:
    batch, catalog, payload, refs = _mixed_model_estimate_fixture()
    fact = next(fact for fact in catalog.facts if fact.evidence_ref == refs[0])
    shifted = fact.model_copy(update={
        "time_range": fact.time_range.model_copy(update={
            "start": fact.time_range.start + timedelta(minutes=1),
        }),
    })
    catalog = rebuild_catalog(catalog, facts=tuple(
        shifted if item.evidence_ref == refs[0] else item
        for item in catalog.facts
    ))
    payload["cells"][0]["value_lower"] = None
    payload["cells"][0]["value_upper"] = None

    with pytest.raises(DraftValidationError):
        DraftValidator(catalog).validate_batch(batch, payload)


@pytest.mark.parametrize("case", (
    "only_one_missing", "explicit_null", "wrong_unit", "wrong_outcome",
    "out_of_scope_ref", "no_range_ref", "changed_bound",
))
def test_missing_range_bounds_are_not_recovered_without_exact_sealed_match(
    case: str,
) -> None:
    batch, catalog, payload, _result_ref = _range_derived_fixture()
    cell = payload["cells"][1]
    if case == "only_one_missing":
        del cell["value_lower"]
    elif case == "explicit_null":
        cell["value_lower"] = None
        del cell["value_upper"]
    elif case == "wrong_unit":
        cell["target_unit_id"] = "wrong-unit"
        del cell["value_lower"]
        del cell["value_upper"]
    elif case == "wrong_outcome":
        cell["outcome_status"] = "quantified"
        del cell["value_lower"]
        del cell["value_upper"]
    elif case == "out_of_scope_ref":
        cell["evidence_refs"].append(batch.cells[0].allowed_evidence_refs[0])
        del cell["value_lower"]
        del cell["value_upper"]
    elif case == "no_range_ref":
        cell["evidence_refs"] = [batch.cells[0].allowed_evidence_refs[0]]
        del cell["value_lower"]
        del cell["value_upper"]
    else:
        cell["value_lower"] = "4"

    with pytest.raises(DraftValidationError):
        DraftValidator(catalog).validate_batch(batch, payload)


def test_multiple_cited_range_facts_do_not_fill_missing_bounds() -> None:
    batch, catalog, payload, result_ref = _range_derived_fixture()
    first = next(fact for fact in catalog.facts if fact.evidence_ref == result_ref)
    second_ref = "e:second-range"
    second = first.model_copy(update={
        "evidence_ref": second_ref,
        "item_id": f"item:{second_ref}",
        "value_lower": "4",
        "value_upper": "10",
    })
    scopes = tuple(
        scope.model_copy(update={
            "target_indirect_refs": tuple(sorted((result_ref, second_ref))),
        }) if scope.cell_id == batch.cells[1].cell_id else scope
        for scope in catalog.cell_scopes
    )
    catalog = rebuild_catalog(
        catalog, facts=(*catalog.facts, second), cell_scopes=scopes,
    )
    planned = batch.cells[1].model_copy(update={
        "allowed_evidence_refs": tuple(sorted((result_ref, second_ref))),
    })
    batch = batch.model_copy(update={
        "cells": (*batch.cells[:1], planned, *batch.cells[2:]),
    })
    payload["cells"][1]["evidence_refs"] = [result_ref, second_ref]
    del payload["cells"][1]["value_lower"]
    del payload["cells"][1]["value_upper"]

    with pytest.raises(DraftValidationError):
        DraftValidator(catalog).validate_batch(batch, payload)


def test_overlapping_but_different_range_fact_window_does_not_fill_bounds() -> None:
    batch, catalog, payload, result_ref = _range_derived_fixture()
    fact = next(fact for fact in catalog.facts if fact.evidence_ref == result_ref)
    shifted = fact.model_copy(update={
        "time_range": fact.time_range.model_copy(update={
            "start": fact.time_range.start + timedelta(minutes=1),
        }),
    })
    catalog = rebuild_catalog(catalog, facts=tuple(
        shifted if item.evidence_ref == result_ref else item
        for item in catalog.facts
    ))
    del payload["cells"][1]["value_lower"]
    del payload["cells"][1]["value_upper"]

    with pytest.raises(DraftValidationError):
        DraftValidator(catalog).validate_batch(batch, payload)


def test_saved_numeric_derivation_supports_exact_value_without_citing_input_cell():
    batch, catalog, payload, input_ref, result_ref = _numeric_derived_fixture()
    validated = DraftValidator(catalog).validate_batch(batch, payload)
    assert validated.cells[1].representative_value == "7"
    assert validated.cells[1].source.value == "inferred"
    assert validated.cells[1].evidence_refs == (result_ref,)
    assert input_ref not in validated.cells[1].evidence_refs

    wrong_value = copy.deepcopy(payload)
    wrong_value["cells"][1]["representative_value"] = "8"
    with pytest.raises(DraftValidationError) as raised:
        DraftValidator(catalog).validate_batch(batch, wrong_value)
    assert "quantified_value_not_backed" in _issue_codes(raised.value)

    wrong_unit = copy.deepcopy(payload)
    wrong_unit["cells"][1]["target_unit_id"] = "pH"
    with pytest.raises(DraftValidationError) as raised:
        DraftValidator(catalog).validate_batch(batch, wrong_unit)
    assert "quantified_value_not_backed" in _issue_codes(raised.value)

    overconfident = copy.deepcopy(payload)
    overconfident["cells"][1]["confidence"] = 0.61
    with pytest.raises(DraftValidationError) as raised:
        DraftValidator(catalog).validate_batch(batch, overconfident)
    assert "numeric_confidence_too_high" in _issue_codes(raised.value)

    wrong_source = copy.deepcopy(payload)
    wrong_source["cells"][1]["source"] = "observed"
    with pytest.raises(DraftValidationError) as raised:
        DraftValidator(catalog).validate_batch(batch, wrong_source)
    assert "numeric_derivation_requires_inferred" in _issue_codes(raised.value)


def test_quality_flags_are_normalized_from_cited_facts_and_logged(caplog) -> None:
    batch, catalog, payload, _input_ref, result_ref = _numeric_derived_fixture()
    facts = tuple(
        fact.model_copy(update={
            "quality_flags": (
                DataQualityFlag.ESTIMATED,
                DataQualityFlag.PROVIDER_FLAGGED,
            ),
        }) if fact.evidence_ref == result_ref else fact
        for fact in catalog.facts
    )
    catalog = rebuild_catalog(catalog, facts=facts)
    payload["cells"][1]["quality_flags"] = [
        "estimated", "partial_coverage", "provider_flagged",
    ]

    with caplog.at_level("INFO", logger="wateragents.reasoning"):
        validated = DraftValidator(catalog).validate_batch(batch, payload)

    assert validated.cells[1].quality_flags == (
        DataQualityFlag.ESTIMATED,
        DataQualityFlag.PROVIDER_FLAGGED,
    )
    assert payload["cells"][1]["quality_flags"] == [
        "estimated", "partial_coverage", "provider_flagged",
    ]
    corrections = [
        record for record in caplog.records
        if getattr(record, "event_name", None)
        == "reasoning_draft_quality_flags_normalized"
    ]
    assert len(corrections) == 1
    assert corrections[0].schema_path == "$.cells[1].quality_flags"
    assert corrections[0].schema_rule == "cited_evidence_union"


def test_quality_flag_normalization_does_not_accept_bad_references_or_enums() -> None:
    batch, catalog, payload, _input_ref, _result_ref = _numeric_derived_fixture()
    validator = DraftValidator(catalog)

    out_of_scope = copy.deepcopy(payload)
    out_of_scope["cells"][1]["quality_flags"] = ["partial_coverage"]
    out_of_scope["cells"][1]["evidence_refs"].append(
        batch.cells[0].allowed_evidence_refs[0]
    )
    with pytest.raises(DraftValidationError) as raised:
        validator.validate_batch(batch, out_of_scope)
    assert "evidence_out_of_scope" in _issue_codes(raised.value)

    invalid_enum = copy.deepcopy(payload)
    invalid_enum["cells"][1]["quality_flags"] = ["not_a_quality_flag"]
    with pytest.raises(DraftValidationError) as raised:
        validator.validate_batch(batch, invalid_enum)
    assert "invalid_enum" in _issue_codes(raised.value)


def test_numeric_derivation_catalog_rejects_wrong_saved_input_identity_and_cap():
    _batch, catalog, _payload, _input_ref, result_ref = _numeric_derived_fixture()
    for update in (
        {"numeric_input_bindings": (("e:0:0", "item:wrong", "w000"),)},
        {"numeric_confidence_cap": 1.1},
        {"numeric_plan_item_id": None},
    ):
        facts = tuple(
            fact.model_copy(update=update) if fact.evidence_ref == result_ref else fact
            for fact in catalog.facts
        )
        with pytest.raises(ValueError, match="numeric derivation"):
            rebuild_catalog(catalog, facts=facts)


def _low_basis_flow_support_fixture():
    _skeleton, plan, catalog, _validator = m7_fixture()
    batch = plan.batch_plan.batches[0]
    target_ref = batch.cells[0].allowed_evidence_refs[0]
    target = next(fact for fact in catalog.facts if fact.evidence_ref == target_ref)
    flow_ref = "e:low-basis-flow-range"
    flow = target.model_copy(update={
        "evidence_ref": flow_ref,
        "item_id": f"item:{flow_ref}",
        "parameter_refs": (ParameterKey(code_system="usgs", code="00060"),),
        "applicability": EvidenceApplicability.ALLOWED_INDIRECT,
        "result_eligibility": EvidenceResultEligibility.INFERRED_ONLY,
        "payload_type": "numeric_derived",
        "value": None,
        "value_lower": "5",
        "value_upper": "50",
        "method_kind": "model_estimate",
        "inference_basis": "general_knowledge",
        "unit_id": "ft3/s",
        "source_record_refs": (),
        "series_ref": "series:flow",
        "quality_class": EvidenceQualityClass.SUSPICIOUS,
        "quality_flags": (DataQualityFlag.ESTIMATED,),
        "numeric_method_id": "test:flow_estimate",
        "numeric_method_version": 1,
        "numeric_task_id": "task:test:flow",
        "numeric_plan_digest": "sha256:test-flow-plan",
        "numeric_plan_item_id": "item:test-flow-plan",
        "numeric_method_registry_digest": "sha256:test-registry",
        "numeric_confidence_cap": 0.1,
    })
    scopes = tuple(
        scope.model_copy(update={"support_indirect_refs": (flow_ref,)})
        if scope.cell_id == batch.cells[0].cell_id else scope
        for scope in catalog.cell_scopes
    )
    combined = rebuild_catalog(
        catalog, facts=(*catalog.facts, flow), cell_scopes=scopes,
    )
    planned = batch.cells[0].model_copy(update={
        "allowed_evidence_refs": tuple(sorted((target_ref, flow_ref))),
    })
    return batch, combined, planned, target, flow


def test_low_basis_flow_range_does_not_cap_direct_target_confidence() -> None:
    batch, combined, planned, target, flow = _low_basis_flow_support_fixture()
    validator = DraftValidator(combined)
    target_ref, flow_ref = target.evidence_ref, flow.evidence_ref
    groups = validator.required_support_reference_groups((planned.cell_id,))
    assert len(groups) == 1 and groups[0].reference_options == (flow_ref,)
    cell = ReasoningCellDraft.model_validate(batch_payload(batch, combined)["cells"][0])
    issues = []
    validator._validate_cell(cell, planned=planned, path="$.cell", issues=issues)
    assert {issue.code for issue in issues} == {"derived_discharge_support_missing"}

    complete = cell.model_copy(update={
        "evidence_refs": tuple(sorted((target_ref, flow_ref))),
        "series_refs": tuple(sorted((target.series_ref, flow.series_ref))),
        "quality_flags": (DataQualityFlag.ESTIMATED,),
    })
    issues = []
    validator._validate_cell(complete, planned=planned, path="$.cell", issues=issues)
    assert complete.confidence == 0.8
    assert issues == []


@pytest.mark.parametrize("derived_type", ["numeric_derived", "temporal_estimate"])
def test_cited_estimate_does_not_change_source_of_selected_direct_value(
    derived_type: str,
):
    skeleton, plan, catalog, _validator = m7_fixture(multi_series_first=True)
    batch = plan.batch_plan.batches[0]
    direct_ref = batch.cells[0].allowed_evidence_refs[0]
    derived_ref = "e:multi"
    anchor_ref = batch.cells[1].allowed_evidence_refs[0]
    facts_by_ref = {fact.evidence_ref: fact for fact in catalog.facts}
    derived = facts_by_ref[derived_ref].model_copy(update={
        "payload_type": derived_type,
        "applicability": EvidenceApplicability.ALLOWED_INDIRECT,
        "result_eligibility": EvidenceResultEligibility.INFERRED_ONLY,
        "value": "8",
        "source_record_refs": (),
        "dependency_evidence_refs": (anchor_ref,),
        "quality_flags": (
            (DataQualityFlag.ESTIMATED,)
            if derived_type == "numeric_derived"
            else (DataQualityFlag.TEMPORAL_ESTIMATE,)
        ),
        **(
            {
                "numeric_method_id": "test:estimate",
                "numeric_method_version": 1,
                "numeric_task_id": "task:test:first",
                "numeric_plan_digest": "sha256:test-plan",
                "numeric_plan_item_id": "item:test-plan",
                "numeric_method_registry_digest": "sha256:test-registry",
                "numeric_confidence_cap": 0.6,
                "numeric_input_bindings": ((
                    anchor_ref,
                    facts_by_ref[anchor_ref].item_id,
                    batch.cells[1].window_id,
                ),),
            }
            if derived_type == "numeric_derived"
            else {
                "series_ref": "series:one-station",
                "location_comid": skeleton.target_comid,
                "station_id": "site:one",
                "temporal_method": "nearest_hold",
                "anchor_distance_seconds": (21600,),
            }
        ),
    })
    anchor = facts_by_ref[anchor_ref]
    if derived_type == "temporal_estimate":
        anchor = anchor.model_copy(update={
            "payload_type": "aggregate",
            "result_eligibility": EvidenceResultEligibility.INFERRED_ONLY,
            "aggregation_method": AggregationMethod.MEAN,
            "series_ref": "series:one-station",
            "location_comid": skeleton.target_comid,
            "station_id": "site:one",
        })
    scopes = tuple(
        scope.model_copy(update={
            "target_direct_refs": (direct_ref,),
            "target_indirect_refs": (derived_ref,),
        }) if scope.cell_id == batch.cells[0].cell_id else scope
        for scope in catalog.cell_scopes
    )
    catalog = rebuild_catalog(
        catalog,
        facts=tuple(
            derived if fact.evidence_ref == derived_ref else
            anchor if fact.evidence_ref == anchor_ref else fact
            for fact in catalog.facts
        ),
        cell_scopes=scopes,
    )
    payload = batch_payload(batch, catalog)
    payload["cells"][0].update(
        evidence_refs=[direct_ref, derived_ref],
        series_refs=sorted((facts_by_ref[direct_ref].series_ref, derived.series_ref)),
        quality_flags=[flag.value for flag in derived.quality_flags],
        confidence=0.8,
    )
    if derived_type == "temporal_estimate":
        payload["cells"][1].update(source="inferred", aggregation_method="mean")

    validator = DraftValidator(catalog)
    observed = validator.validate_batch(batch, payload)
    assert observed.cells[0].source.value == "observed"
    assert observed.cells[0].representative_value == "7"

    selected_derived = copy.deepcopy(payload)
    selected_derived["cells"][0]["representative_value"] = "8"
    selected_derived["cells"][0]["source"] = "inferred"
    selected_derived["cells"][0]["confidence"] = 0.5
    assert validator.validate_batch(batch, selected_derived).cells[0].source.value == "inferred"

    wrong_source = copy.deepcopy(selected_derived)
    wrong_source["cells"][0]["source"] = "observed"
    with pytest.raises(DraftValidationError) as raised:
        validator.validate_batch(batch, wrong_source)
    assert (
        "numeric_derivation_requires_inferred"
        if derived_type == "numeric_derived"
        else "temporal_estimate_requires_inferred"
    ) in _issue_codes(raised.value)


def test_partial_suspicious_aggregate_supports_quantified_inferred_mean() -> None:
    _skeleton, plan, catalog, _validator = m7_fixture(
        supported=lambda _parameter_index, window_index: window_index == 0
    )
    batch = plan.batch_plan.batches[0]
    evidence_ref = batch.cells[0].allowed_evidence_refs[0]
    aggregate_fact = next(
        fact for fact in catalog.facts if fact.evidence_ref == evidence_ref
    ).model_copy(
        update={
            "result_eligibility": EvidenceResultEligibility.INFERRED_ONLY,
            "payload_type": "aggregate",
            "value": "8.2",
            "aggregation_method": AggregationMethod.MEAN,
            "source_record_refs": ("source:reading-1", "source:reading-2"),
            "quality_class": EvidenceQualityClass.SUSPICIOUS,
            "quality_flags": (
                DataQualityFlag.PARTIAL_COVERAGE,
                DataQualityFlag.PROVIDER_FLAGGED,
            ),
        }
    )
    aggregate_catalog = rebuild_catalog(
        catalog,
        facts=tuple(
            aggregate_fact if fact.evidence_ref == evidence_ref else fact
            for fact in catalog.facts
        ),
    )
    validator = DraftValidator(aggregate_catalog)
    payload = batch_payload(batch, aggregate_catalog)
    payload["cells"][0].update(
        source="inferred",
        trend="uncertain",
        risk_level="indeterminate",
        confidence=0.2,
        aggregation_method="mean",
        reason_codes=[],
        quality_flags=["partial_coverage", "provider_flagged"],
    )

    validated = validator.validate_batch(batch, payload)
    assert validated.cells[0].representative_value == "8.2"
    assert validated.cells[0].source.value == "inferred"
    assert validated.cells[0].series_refs == (aggregate_fact.series_ref,)
    assert validated.cells[0].reason_codes == ()
    assert validated.cells[0].quality_flags == aggregate_fact.quality_flags

    wrong_series = copy.deepcopy(payload)
    wrong_series["cells"][0]["series_refs"] = [evidence_ref]
    with pytest.raises(DraftValidationError) as raised:
        validator.validate_batch(batch, wrong_series)
    assert "series_refs_incomplete" in _issue_codes(raised.value)


def _temporal_estimate_fixture(
    *,
    method: str,
    distance_seconds: int,
    anchor_aggregation_method: AggregationMethod = AggregationMethod.MEAN,
):
    skeleton, plan, catalog, _validator = m7_fixture()
    batch = plan.batch_plan.batches[0]
    estimate_ref = batch.cells[1].allowed_evidence_refs[0]
    anchor_indexes = (0, 2) if method == "linear_interpolation" else (0,)
    anchor_refs = tuple(batch.cells[index].allowed_evidence_refs[0] for index in anchor_indexes)
    facts = []
    for fact in catalog.facts:
        if fact.evidence_ref in anchor_refs:
            facts.append(fact.model_copy(update={
                "payload_type": "aggregate",
                "result_eligibility": EvidenceResultEligibility.INFERRED_ONLY,
                "aggregation_method": anchor_aggregation_method,
                "value": "9" if fact.evidence_ref == anchor_refs[-1] and len(anchor_refs) == 2 else "7",
                "series_ref": "series:one-station",
                "location_comid": skeleton.target_comid,
                "station_id": "site:one",
            }))
        elif fact.evidence_ref == estimate_ref:
            facts.append(fact.model_copy(update={
                "payload_type": "temporal_estimate",
                "applicability": EvidenceApplicability.ALLOWED_INDIRECT,
                "result_eligibility": EvidenceResultEligibility.INFERRED_ONLY,
                "value": "8" if len(anchor_refs) == 2 else "7",
                "source_record_refs": (),
                "dependency_evidence_refs": anchor_refs,
                "series_ref": "series:one-station",
                "location_comid": skeleton.target_comid,
                "station_id": "site:one",
                "quality_flags": (DataQualityFlag.TEMPORAL_ESTIMATE,),
                "temporal_method": method,
                "anchor_distance_seconds": (distance_seconds,) * len(anchor_refs),
            }))
        else:
            facts.append(fact)
    scopes = tuple(
        scope.model_copy(update={
            "target_direct_refs": (),
            "target_indirect_refs": (estimate_ref,),
        }) if scope.cell_id == batch.cells[1].cell_id else scope
        for scope in catalog.cell_scopes
    )
    estimate_catalog = rebuild_catalog(catalog, facts=tuple(facts), cell_scopes=scopes)
    payload = batch_payload(batch, estimate_catalog)
    for index in anchor_indexes:
        if anchor_aggregation_method is AggregationMethod.LATEST:
            payload["cells"][index].update(
                outcome_status="directional",
                representative_value=None,
                target_unit_id=None,
                source="inferred",
                trend="rising",
                risk_level="medium",
                aggregation_method=None,
            )
        else:
            payload["cells"][index].update(source="inferred", aggregation_method="mean")
    estimate = payload["cells"][1]
    estimate.update(
        source="inferred",
        aggregation_method=None,
        confidence=0.5,
        confidence_rationale=f"{method} from anchors {distance_seconds} seconds away.",
        quality_flags=["temporal_estimate"],
    )
    return skeleton, plan, batch, estimate_catalog, payload, estimate_ref, anchor_refs


@pytest.mark.parametrize(
    "anchor_aggregation_method",
    (AggregationMethod.MEAN, AggregationMethod.LATEST),
)
def test_typed_temporal_estimate_quantifies_without_citing_cross_window_anchors(
    anchor_aggregation_method: AggregationMethod,
) -> None:
    _skeleton, _plan, batch, catalog, payload, estimate_ref, anchor_refs = _temporal_estimate_fixture(
        method="linear_interpolation",
        distance_seconds=21600,
        anchor_aggregation_method=anchor_aggregation_method,
    )
    result = DraftValidator(catalog).validate_batch(batch, payload)
    assert result.cells[1].representative_value == "8"
    assert result.cells[1].evidence_refs == (estimate_ref,)
    assert result.cells[1].source.value == "inferred"
    assert not set(anchor_refs) & set(result.cells[1].evidence_refs)
    assert {
        fact.aggregation_method
        for fact in catalog.facts
        if fact.evidence_ref in anchor_refs
    } == {anchor_aggregation_method}

    wrong_source = copy.deepcopy(payload)
    wrong_source["cells"][1]["source"] = "observed"
    with pytest.raises(DraftValidationError) as raised:
        DraftValidator(catalog).validate_batch(batch, wrong_source)
    assert "temporal_estimate_requires_inferred" in _issue_codes(raised.value)

    overconfident = copy.deepcopy(payload)
    overconfident["cells"][1]["confidence"] = 0.8
    with pytest.raises(DraftValidationError) as raised:
        DraftValidator(catalog).validate_batch(batch, overconfident)
    assert "temporal_confidence_too_high" in _issue_codes(raised.value)


def test_temporal_estimate_rejects_unsupported_anchor_aggregation() -> None:
    with pytest.raises(ValueError, match="matching measured windows"):
        _temporal_estimate_fixture(
            method="linear_interpolation",
            distance_seconds=21600,
            anchor_aggregation_method=AggregationMethod.MEDIAN,
        )


def test_far_one_sided_temporal_estimate_has_lower_confidence_cap() -> None:
    _skeleton, _plan, batch, catalog, payload, _estimate_ref, _anchor_refs = _temporal_estimate_fixture(
        method="nearest_hold", distance_seconds=43200,
    )
    payload["cells"][1]["confidence"] = 0.25
    DraftValidator(catalog).validate_batch(batch, payload)
    payload["cells"][1]["confidence"] = 0.3
    with pytest.raises(DraftValidationError) as raised:
        DraftValidator(catalog).validate_batch(batch, payload)
    assert "temporal_confidence_too_high" in _issue_codes(raised.value)


def test_temporal_estimate_rejects_anchor_from_other_station() -> None:
    _skeleton, _plan, _batch, catalog, _payload, _estimate_ref, anchor_refs = _temporal_estimate_fixture(
        method="linear_interpolation", distance_seconds=21600,
    )
    facts = tuple(
        fact.model_copy(update={"station_id": "site:other"})
        if fact.evidence_ref == anchor_refs[0] else fact
        for fact in catalog.facts
    )
    with pytest.raises(ValueError, match="matching measured windows"):
        rebuild_catalog(catalog, facts=facts)


def test_only_temporal_discharge_support_must_be_cited() -> None:
    skeleton, _plan, batch, catalog, payload, target_ref, anchor_refs = (
        _temporal_estimate_fixture(
            method="linear_interpolation", distance_seconds=21600,
        )
    )
    flow_parameter = ParameterKey(code_system="usgs", code="00060")
    facts_by_ref = {fact.evidence_ref: fact for fact in catalog.facts}
    flow_anchor_refs = tuple(f"{ref}:flow" for ref in anchor_refs)
    flow_anchors = tuple(
        facts_by_ref[ref].model_copy(update={
            "evidence_ref": flow_ref,
            "item_id": f"item:{flow_ref}",
            "parameter_refs": (flow_parameter,),
            "series_ref": "series:flow",
        })
        for ref, flow_ref in zip(anchor_refs, flow_anchor_refs, strict=True)
    )
    flow_ref = "e:flow-estimate"
    flow_estimate = facts_by_ref[target_ref].model_copy(update={
        "evidence_ref": flow_ref,
        "item_id": f"item:{flow_ref}",
        "parameter_refs": (flow_parameter,),
        "series_ref": "series:flow",
        "dependency_evidence_refs": flow_anchor_refs,
        "value": "20",
    })
    scopes = tuple(
        scope.model_copy(update={"support_indirect_refs": (flow_ref,)})
        if scope.cell_id == batch.cells[1].cell_id else scope
        for scope in catalog.cell_scopes
    )
    combined = rebuild_catalog(
        catalog,
        facts=(*catalog.facts, *flow_anchors, flow_estimate),
        cell_scopes=scopes,
    )
    validator = DraftValidator(combined)
    planned = batch.cells[1].model_copy(update={
        "allowed_evidence_refs": tuple(sorted((target_ref, flow_ref))),
    })
    cell = ReasoningCellDraft.model_validate(payload["cells"][1])
    issues = []
    validator._validate_cell(cell, planned=planned, path="$.cell", issues=issues)
    assert {issue.code for issue in issues} == {"temporal_discharge_support_missing"}

    complete = cell.model_copy(update={
        "evidence_refs": tuple(sorted((target_ref, flow_ref))),
        "series_refs": ("series:flow", "series:one-station"),
        "confidence": 0.75,
    })
    issues = []
    validator._validate_cell(complete, planned=planned, path="$.cell", issues=issues)
    assert issues == []
    estimated_point = _point_from_draft(complete, skeleton, combined)
    assert estimated_point.temporal_estimate_method == "linear_interpolation"
    assert estimated_point.confidence == 0.75
    issues = []
    validator._validate_cell(
        complete.model_copy(update={"confidence": 0.76}),
        planned=planned,
        path="$.cell",
        issues=issues,
    )
    assert {issue.code for issue in issues} == {"temporal_confidence_too_high"}

    measured_target = facts_by_ref[target_ref].model_copy(update={
        "payload_type": "aggregate",
        "applicability": EvidenceApplicability.DIRECT_MEASUREMENT,
        "result_eligibility": EvidenceResultEligibility.INFERRED_ONLY,
        "aggregation_method": AggregationMethod.MEAN,
        "source_record_refs": ("source:pH",),
        "dependency_evidence_refs": (),
        "quality_class": EvidenceQualityClass.SUSPICIOUS,
        "quality_flags": (DataQualityFlag.PARTIAL_COVERAGE,),
        "temporal_method": None,
        "anchor_distance_seconds": (),
    })
    measured_scopes = tuple(
        scope.model_copy(update={
            "target_direct_refs": (target_ref,),
            "target_indirect_refs": (),
        }) if scope.cell_id == batch.cells[1].cell_id else scope
        for scope in scopes
    )
    measured_catalog = rebuild_catalog(
        combined,
        facts=tuple(
            measured_target if fact.evidence_ref == target_ref else fact
            for fact in combined.facts
        ),
        cell_scopes=measured_scopes,
    )
    measured_cell = complete.model_copy(update={
        "aggregation_method": AggregationMethod.MEAN,
        "quality_flags": (
            DataQualityFlag.PARTIAL_COVERAGE,
            DataQualityFlag.TEMPORAL_ESTIMATE,
        ),
    })
    issues = []
    DraftValidator(measured_catalog)._validate_cell(
        measured_cell, planned=planned, path="$.cell", issues=issues,
    )
    assert issues == []
    measured_point = _point_from_draft(measured_cell, skeleton, measured_catalog)
    assert measured_point.temporal_estimate_method is None
    assert DataQualityFlag.TEMPORAL_ESTIMATE in measured_point.quality_flags


@pytest.mark.parametrize(
    "field_name", ("risk_rationale", "confidence_rationale", "explanation")
)
def test_cell_text_requires_its_own_structured_ref_for_known_evidence(
    field_name: str,
) -> None:
    _skeleton, plan, catalog, _validator = m7_fixture()
    batch = plan.batch_plan.batches[0]
    flow_ref = "bf5b36ff-9ed6-44d4-b6d1-c3d0ef660f75:aggregate"
    flow_fact = catalog.facts[0].model_copy(
        update={"evidence_ref": flow_ref, "item_id": "item:flow"}
    )
    validator = DraftValidator(
        rebuild_catalog(catalog, facts=(*catalog.facts, flow_fact))
    )
    payload = batch_payload(batch, catalog)
    payload["cells"][0][field_name] = (
        f"The discharge aggregate {flow_ref} cannot quantify pH."
    )

    with pytest.raises(DraftValidationError) as raised:
        validator.validate_batch(batch, payload)
    assert any(
        issue.code == "text_evidence_ref_missing"
        and issue.path == f"$.cells[0].{field_name}"
        for issue in raised.value.issues
    )

    unauthorized = copy.deepcopy(payload)
    unauthorized["cells"][0]["evidence_refs"].append(flow_ref)
    with pytest.raises(DraftValidationError) as raised:
        validator.validate_batch(batch, unauthorized)
    assert "evidence_out_of_scope" in _issue_codes(raised.value)

    payload["cells"][0][field_name] = f"See {flow_ref}:suffix for context."
    validator.validate_batch(batch, payload)


def test_cell_text_accepts_known_evidence_when_cell_cites_it() -> None:
    _skeleton, plan, catalog, validator = m7_fixture(multi_series_first=True)
    batch = plan.batch_plan.batches[0]
    refs = list(batch.cells[0].allowed_evidence_refs)
    facts = {fact.evidence_ref: fact for fact in catalog.facts}
    payload = batch_payload(batch, catalog)
    payload["cells"][0].update(
        source="inferred",
        evidence_refs=refs,
        series_refs=sorted(facts[ref].series_ref for ref in refs),
        risk_rationale=f"The cited record {refs[-1]} adds supporting context.",
    )

    validator.validate_batch(batch, payload)


def test_multi_series_must_be_inferred_and_preserve_all_series_facts() -> None:
    _skeleton, plan, catalog, validator = m7_fixture(multi_series_first=True)
    batch = plan.batch_plan.batches[0]
    payload = batch_payload(batch, catalog)
    first = payload["cells"][0]
    refs = list(batch.cells[0].allowed_evidence_refs)
    facts = {fact.evidence_ref: fact for fact in catalog.facts}
    first.update(
        source="inferred",
        evidence_refs=refs,
        series_refs=sorted(facts[ref].series_ref for ref in refs),
    )
    validator.validate_batch(batch, payload)

    observed = copy.deepcopy(payload)
    observed["cells"][0]["source"] = "observed"
    with pytest.raises(DraftValidationError) as raised:
        validator.validate_batch(batch, observed)
    assert "observed_not_one_to_one" in _issue_codes(raised.value)

    missing_series = copy.deepcopy(payload)
    missing_series["cells"][0]["series_refs"].pop()
    with pytest.raises(DraftValidationError) as raised:
        validator.validate_batch(batch, missing_series)
    assert "series_refs_incomplete" in _issue_codes(raised.value)


def test_observed_allows_only_registered_reversible_display_conversion() -> None:
    _skeleton, plan, catalog, validator = m7_fixture(conversion_first=True)
    batch = plan.batch_plan.batches[0]
    payload = batch_payload(batch, catalog)
    first = payload["cells"][0]
    direct_ref = batch.cells[0].allowed_evidence_refs[0]
    conversion_ref = "e:conversion"
    facts = {fact.evidence_ref: fact for fact in catalog.facts}
    first.update(
        representative_value=facts[conversion_ref].value,
        target_unit_id=facts[conversion_ref].unit_id,
        source="observed",
        evidence_refs=[direct_ref, conversion_ref],
        series_refs=[facts[conversion_ref].series_ref],
    )
    validator.validate_batch(batch, payload)

    unregistered_facts = tuple(
        fact.model_copy(update={"registered_reversible_conversion": False})
        if fact.evidence_ref == conversion_ref
        else fact
        for fact in catalog.facts
    )
    unregistered = rebuild_catalog(catalog, facts=unregistered_facts)
    with pytest.raises(DraftValidationError) as raised:
        DraftValidator(unregistered).validate_batch(batch, payload)
    assert "observed_requires_direct_record" in _issue_codes(raised.value)


def test_prediction_requires_predicted_source() -> None:
    _skeleton, plan, catalog, validator = m7_fixture(prediction=True)
    batch = plan.batch_plan.batches[0]
    payload = batch_payload(batch, catalog)
    for cell in payload["cells"]:
        cell["source"] = "predicted"
    validator.validate_batch(batch, payload)

    payload["cells"][0]["source"] = "inferred"
    with pytest.raises(DraftValidationError) as raised:
        validator.validate_batch(batch, payload)
    assert "prediction_source_required" in _issue_codes(raised.value)


def test_synthesis_is_complete_strict_and_checks_attribution_basis() -> None:
    skeleton, plan, catalog, validator = m7_fixture(attribution_context=True)
    reasoning = validated_reasoning(plan, validator, catalog)
    valid = synthesis_payload(skeleton, plan, reasoning)

    result = validator.validate_synthesis(
        skeleton,
        plan.batch_plan,
        plan.synthesis_plan,
        reasoning,
        valid,
    )
    assert len(result.window_assessments) == len(skeleton.windows)

    corruptions = []
    missing_window = copy.deepcopy(valid)
    missing_window["window_assessments"].pop()
    corruptions.append(missing_window)
    duplicate_window = copy.deepcopy(valid)
    duplicate_window["window_assessments"].append(
        copy.deepcopy(duplicate_window["window_assessments"][0])
    )
    corruptions.append(duplicate_window)
    wrong_overall_windows = copy.deepcopy(valid)
    wrong_overall_windows["overall"]["contributing_window_refs"].pop()
    corruptions.append(wrong_overall_windows)
    unknown_ref = copy.deepcopy(valid)
    unknown_ref["analysis_detail"]["steps"][0]["evidence_refs"] = ["e:unknown"]
    corruptions.append(unknown_ref)
    availability = copy.deepcopy(valid)
    availability["overall"]["availability"] = "complete"
    corruptions.append(availability)
    no_evidence_none = copy.deepcopy(valid)
    no_evidence_none["overall"].update(risk_level="none", evidence_refs=[])
    corruptions.append(no_evidence_none)
    bad_correlated = copy.deepcopy(valid)
    bad_correlated["attribution_summary"]["primary_factors"][0][
        "basis"
    ] = AttributionBasis.CORRELATED.value
    corruptions.append(bad_correlated)
    bad_hypothesis = copy.deepcopy(valid)
    bad_hypothesis["attribution_summary"]["primary_factors"][0][
        "basis"
    ] = AttributionBasis.HYPOTHESIZED.value
    corruptions.append(bad_hypothesis)

    for payload in corruptions:
        with pytest.raises(DraftValidationError):
            validator.validate_synthesis(
                skeleton,
                plan.batch_plan,
                plan.synthesis_plan,
                reasoning,
                payload,
            )

    inferred = copy.deepcopy(valid)
    inferred["attribution_summary"]["primary_factors"][0][
        "basis"
    ] = AttributionBasis.INFERRED.value
    validator.validate_synthesis(
        skeleton,
        plan.batch_plan,
        plan.synthesis_plan,
        reasoning,
        inferred,
    )

    factor = valid["attribution_summary"]["primary_factors"][0]
    direct_ref = factor["evidence_refs"][0]
    correlated = copy.deepcopy(valid)
    correlated_factor = correlated["attribution_summary"]["primary_factors"][0]
    correlated_factor["basis"] = AttributionBasis.CORRELATED.value
    correlated_factor["evidence_refs"] = [direct_ref, "e:relation"]
    validator.validate_synthesis(
        skeleton,
        plan.batch_plan,
        plan.synthesis_plan,
        reasoning,
        correlated,
    )

    hypothesized = copy.deepcopy(valid)
    hypothesis_factor = hypothesized["attribution_summary"]["primary_factors"][0]
    hypothesis_factor["basis"] = AttributionBasis.HYPOTHESIZED.value
    hypothesis_factor["evidence_refs"] = ["e:context"]
    validator.validate_synthesis(
        skeleton,
        plan.batch_plan,
        plan.synthesis_plan,
        reasoning,
        hypothesized,
    )


def test_synthesis_rejects_reasoning_from_another_batch_plan() -> None:
    skeleton, plan, catalog, validator = m7_fixture()
    reasoning = validated_reasoning(plan, validator, catalog)
    payload = synthesis_payload(skeleton, plan, reasoning)
    wrong_reasoning = reasoning.model_copy(
        update={"batch_plan_digest": "sha256:another-plan"}
    )

    with pytest.raises(DraftValidationError) as raised:
        validator.validate_synthesis(
            skeleton,
            plan.batch_plan,
            plan.synthesis_plan,
            wrong_reasoning,
            payload,
        )
    assert "reasoning_plan_mismatch" in _issue_codes(raised.value)


def test_real_m5_sealed_fragments_build_the_validation_catalog() -> None:
    async def arrange():
        validated, handoff, registry = await _validated_fixture(mode="full")
        sealed = seal_evidence_input(validated, handoff, registry)
        fragments = tuple(
            [
                await handoff.read_port.read_fragment(
                    validated.task.evidence_scope,
                    reference,
                )
                for reference in sealed.fragment_allowlist
            ]
        )
        return sealed, fragments

    sealed, fragments = asyncio.run(arrange())
    catalog = build_reasoning_evidence_catalog(sealed, fragments)

    assert tuple(fact.evidence_ref for fact in catalog.facts) == tuple(
        sorted(reference.evidence_id for reference in sealed.fragment_allowlist)
    )
    assert tuple(scope.cell_id for scope in catalog.cell_scopes) == tuple(
        gate.cell_id for gate in sealed.gates
    )
