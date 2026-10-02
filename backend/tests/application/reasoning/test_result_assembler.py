import pytest

from app.application.reasoning import (
    DraftValidator,
    assemble_inference_result,
)
from app.domain import (
    AssessmentAvailability,
    EvidenceReference,
    IndeterminateReason,
    ModelExecutionSummary,
    ModelSelection,
    OutcomeStatus,
    RiskLevel,
    WaterQualityParameter,
)
from tests.application.reasoning.m7_support import (
    batch_payload,
    m7_fixture,
    synthesis_payload,
    validated_reasoning,
)


MODEL = ModelSelection(provider="test-provider", model="waterbody-model")


def _parameters(skeleton):
    return tuple(
        WaterQualityParameter(
            key=key,
            name=f"Parameter {key.code}",
            canonical_unit="mg/l",
            category="test",
        )
        for key in skeleton.requested_parameters
    )


def _references(skeleton):
    return tuple(
        EvidenceReference(
            evidence_id=evidence_id,
            role="sealed_evidence",
            summary=None,
        )
        for evidence_id in skeleton.evidence_allowlist
    )


def _summary(skeleton, *, batches: int, synthesis: int):
    return ModelExecutionSummary(
        model=MODEL,
        planning_calls=1,
        tool_calls=2,
        reasoning_batches=batches,
        repair_calls=0,
        synthesis_calls=synthesis,
        skipped_cells=skeleton.fixed_cell_count,
        usage=None,
    )


def _validated_synthesis(skeleton, plan, catalog, validator):
    reasoning = validated_reasoning(plan, validator, catalog)
    synthesis = validator.validate_synthesis(
        skeleton,
        plan.batch_plan,
        plan.synthesis_plan,
        reasoning,
        synthesis_payload(skeleton, plan, reasoning),
    )
    return reasoning, synthesis


def test_assembler_preserves_validated_agent_judgments_and_reference_closure():
    skeleton, plan, catalog, validator = m7_fixture(parameter_count=2)
    reasoning, synthesis = _validated_synthesis(
        skeleton, plan, catalog, validator
    )
    summary = _summary(
        skeleton,
        batches=plan.batch_plan.batch_count,
        synthesis=1,
    )

    result = assemble_inference_result(
        skeleton,
        reasoning=reasoning,
        synthesis=synthesis,
        parameter_definitions=_parameters(skeleton),
        evidence_references=_references(skeleton),
        model_execution_summary=summary,
    )
    repeated = assemble_inference_result(
        skeleton,
        reasoning=reasoning,
        synthesis=synthesis,
        parameter_definitions=_parameters(skeleton),
        evidence_references=_references(skeleton),
        model_execution_summary=summary,
    )

    assert result == repeated
    assert result.model_dump_json() == repeated.model_dump_json()
    assert result.overall_assessment.risk_level is RiskLevel.LOW
    assert result.overall_confidence == synthesis.overall.confidence
    assert tuple(item.parameter.key for item in result.series) == (
        skeleton.requested_parameters
    )
    assert tuple(
        point.cell_id for series in result.series for point in series.points
    ) == skeleton.cell_order
    assert all(
        point.assessment.risk_level is RiskLevel.NONE
        for series in result.series
        for point in series.points
    )
    known_cells = set(skeleton.cell_order)
    assert all(
        set(step.result_cell_refs) <= known_cells
        for step in result.analysis_detail.steps
    )
    assert all(
        set(factor.result_cell_refs) <= known_cells
        for factor in result.attribution_summary.primary_factors
    )
    assert {item.evidence_id for item in result.evidence_chain.references} == set(
        skeleton.evidence_allowlist
    )


def test_assembler_computes_partial_availability_and_blockers_without_risk_rewrite():
    skeleton, plan, catalog, validator = m7_fixture(
        supported=lambda _parameter, window: window not in {1, 2},
        blocked=lambda _parameter, window: window == 1,
    )
    reasoning, synthesis = _validated_synthesis(
        skeleton, plan, catalog, validator
    )
    result = assemble_inference_result(
        skeleton,
        reasoning=reasoning,
        synthesis=synthesis,
        parameter_definitions=_parameters(skeleton),
        evidence_references=_references(skeleton),
        model_execution_summary=_summary(
            skeleton,
            batches=plan.batch_plan.batch_count,
            synthesis=1,
        ),
    )

    assert result.overall_assessment.availability is AssessmentAvailability.PARTIAL
    assert result.overall_assessment.risk_level is synthesis.overall.risk_level
    assert result.overall_assessment.assessed_cell_count == 2
    assert len(result.overall_assessment.missing_cell_refs) == 2
    assert result.overall_assessment.blocker_refs == ("e:0:1",)
    assert result.window_assessments[0].risk_level is RiskLevel.LOW
    assert result.window_assessments[1].availability is (
        AssessmentAvailability.INDETERMINATE
    )
    assert result.window_assessments[1].risk_level is RiskLevel.INDETERMINATE
    assert result.window_assessments[1].blocker_refs == ("e:0:1",)


def test_all_fixed_natural_absence_assembles_full_result_without_model_drafts():
    skeleton, plan, _catalog, _validator = m7_fixture(
        supported=lambda _parameter, _window: False,
    )
    assert not plan.batch_plan.batches
    assert plan.synthesis_plan.requires_model_call is False

    result = assemble_inference_result(
        skeleton,
        reasoning=None,
        synthesis=None,
        parameter_definitions=_parameters(skeleton),
        evidence_references=_references(skeleton),
        model_execution_summary=_summary(skeleton, batches=0, synthesis=0),
    )

    assert result.overall_assessment.availability is (
        AssessmentAvailability.INDETERMINATE
    )
    assert result.overall_assessment.confidence == 0
    assert result.model_execution_summary.reasoning_batches == 0
    assert result.model_execution_summary.synthesis_calls == 0
    assert result.model_execution_summary.skipped_cells == len(skeleton.cell_order)
    assert all(
        point.outcome_status is OutcomeStatus.INDETERMINATE
        and point.assessment.reason_codes
        == (IndeterminateReason.INSUFFICIENT_EVIDENCE,)
        for series in result.series
        for point in series.points
    )


def test_all_agent_indeterminate_remains_indeterminate_at_every_aggregate_layer():
    skeleton, plan, catalog, validator = m7_fixture()
    batches = []
    for batch in plan.batch_plan.batches:
        payload = batch_payload(batch, catalog)
        for cell in payload["cells"]:
            cell.update(
                outcome_status="indeterminate",
                representative_value=None,
                target_unit_id=None,
                source=None,
                trend="uncertain",
                risk_level="indeterminate",
                risk_rationale="The cited evidence does not support a determinate judgment.",
                confidence=0.0,
                confidence_rationale="The water-quality judgment remains indeterminate.",
                aggregation_method=None,
                reason_codes=["insufficient_evidence"],
            )
        batches.append(validator.validate_batch(batch, payload))
    reasoning = validator.complete_batches(plan.batch_plan, tuple(batches))
    payload = synthesis_payload(skeleton, plan, reasoning)
    for series in payload["series_assessments"]:
        series.update(
            confidence=0.0,
            confidence_rationale="All validated cells are indeterminate.",
            contributing_cell_refs=[],
        )
    for window in payload["window_assessments"]:
        window.update(
            risk_level="indeterminate",
            risk_rationale="All validated cells are indeterminate.",
            confidence=0.0,
            confidence_rationale="No supported water-quality judgment is available.",
            contributing_cell_refs=[],
        )
    payload["overall"].update(
        risk_level="indeterminate",
        risk_rationale="All validated cells are indeterminate.",
        confidence=0.0,
        confidence_rationale="No supported water-quality judgment is available.",
        contributing_cell_refs=[],
    )
    payload["attribution_summary"] = {
        "summary": "No determinate result is available for attribution.",
        "primary_factors": [],
    }
    synthesis = validator.validate_synthesis(
        skeleton,
        plan.batch_plan,
        plan.synthesis_plan,
        reasoning,
        payload,
    )

    result = assemble_inference_result(
        skeleton,
        reasoning=reasoning,
        synthesis=synthesis,
        parameter_definitions=_parameters(skeleton),
        evidence_references=_references(skeleton),
        model_execution_summary=_summary(
            skeleton,
            batches=plan.batch_plan.batch_count,
            synthesis=1,
        ),
    )

    assert result.overall_assessment.availability is (
        AssessmentAvailability.INDETERMINATE
    )
    assert result.overall_assessment.risk_level is RiskLevel.INDETERMINATE
    assert result.overall_assessment.confidence == 0
    assert all(
        item.availability is AssessmentAvailability.INDETERMINATE
        for item in result.window_assessments
    )
@pytest.mark.parametrize("risk_level", ["none", "high"])
def test_partial_availability_never_rewrites_validated_none_or_high_risk(
    risk_level,
):
    skeleton, plan, catalog, validator = m7_fixture(
        supported=lambda _parameter, window: window != 1,
    )
    reasoning = validated_reasoning(plan, validator, catalog)
    payload = synthesis_payload(skeleton, plan, reasoning)
    payload["overall"].update(
        risk_level=risk_level,
        risk_rationale="The cited supported cells determine this risk judgment.",
    )
    for window in payload["window_assessments"]:
        if window["contributing_cell_refs"]:
            window.update(
                risk_level=risk_level,
                risk_rationale="The cited supported cell determines this risk judgment.",
            )
    synthesis = validator.validate_synthesis(
        skeleton,
        plan.batch_plan,
        plan.synthesis_plan,
        reasoning,
        payload,
    )

    result = assemble_inference_result(
        skeleton,
        reasoning=reasoning,
        synthesis=synthesis,
        parameter_definitions=_parameters(skeleton),
        evidence_references=_references(skeleton),
        model_execution_summary=_summary(
            skeleton,
            batches=plan.batch_plan.batch_count,
            synthesis=1,
        ),
    )

    assert result.overall_assessment.availability is AssessmentAvailability.PARTIAL
    assert result.overall_assessment.risk_level.value == risk_level
