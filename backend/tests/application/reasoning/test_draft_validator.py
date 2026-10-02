import copy
import asyncio
import json

import pytest

from app.application.reasoning import (
    build_reasoning_evidence_catalog,
    DraftValidationError,
    DraftValidator,
    seal_evidence_input,
)
from app.domain import (
    AggregationMethod,
    AttributionBasis,
    EvidenceApplicability,
    EvidenceResultEligibility,
)
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
        ("quality_flags", ["stale"]),
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
