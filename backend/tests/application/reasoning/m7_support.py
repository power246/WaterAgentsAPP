from __future__ import annotations

from app.application.reasoning import (
    CellEvidenceValidationScope,
    DraftValidator,
    EvidenceValidationFact,
    ReasoningEvidenceCatalog,
    ResultSkeleton,
    ValidatedReasoningSet,
    overall_synthesis_response_schema,
    reasoning_batch_response_schema,
)
from app.application.reasoning.prompt_policy import canonical_json_digest
from app.domain import (
    AttributionBasis,
    EvidenceApplicability,
    EvidenceQualityClass,
    EvidenceResultEligibility,
    InferenceMode,
    ResultSource,
    TimeRange,
)
from tests.application.reasoning.test_batch_planner import (
    DEADLINE,
    _make_skeleton,
    _model_fit,
    _planner,
    _range_for_windows,
    _working_context,
)


def m7_fixture(
    *,
    max_cells: int = 32,
    parameter_count: int = 1,
    supported=lambda _parameter_index, _window_index: True,
    blocked=lambda _parameter_index, _window_index: False,
    multi_series_first: bool = False,
    prediction: bool = False,
    attribution_context: bool = False,
    conversion_first: bool = False,
    unit_preference=None,
):
    original = _make_skeleton(
        parameter_count=parameter_count,
        segment_specs=(
            (
                _range_for_windows(4),
                (
                    InferenceMode.PREDICTION
                    if prediction
                    else InferenceMode.GAP_FILLING
                ),
            ),
        ),
        supported=supported,
        blocked=blocked,
    )
    supported = tuple(
        scope.model_copy(
            update={
                "allowed_result_sources": (
                    (ResultSource.PREDICTED,)
                    if prediction
                    else (ResultSource.OBSERVED, ResultSource.INFERRED)
                ),
                "allowed_evidence_refs": (
                    tuple(
                        sorted(
                            (
                                *scope.allowed_evidence_refs,
                                *(
                                    ("e:multi",)
                                    if multi_series_first and index == 0
                                    else ()
                                ),
                                *(
                                    ("e:conversion",)
                                    if conversion_first and index == 0
                                    else ()
                                ),
                            )
                        )
                    )
                    if (multi_series_first or conversion_first) and index == 0
                    else scope.allowed_evidence_refs
                ),
            }
        )
        for index, scope in enumerate(original.supported_scopes)
    )
    if unit_preference is not None:
        supported = tuple(
            scope.model_copy(update={
                "preferred_target_unit_id": unit_preference.target_unit_id,
                "unit_preference_source": unit_preference.source,
                "unit_registry_digest": unit_preference.registry_digest,
            }) if scope.parameter == unit_preference.parameter else scope
            for scope in supported
        )
    skeleton_values = original.model_dump(
        mode="python",
        exclude={"skeleton_digest"},
    )
    skeleton_values["supported_scopes"] = supported
    if unit_preference is not None:
        skeleton_values["unit_preferences"] = (unit_preference,)
        skeleton_values["capability_registry_digest"] = unit_preference.registry_digest
    if multi_series_first:
        skeleton_values["evidence_allowlist"] = tuple(
            sorted((*skeleton_values["evidence_allowlist"], "e:multi"))
        )
        skeleton_values["source_record_allowlist"] = tuple(
            sorted((*skeleton_values["source_record_allowlist"], "s:multi"))
        )
    if attribution_context:
        skeleton_values["evidence_allowlist"] = tuple(
            sorted(
                (
                    *skeleton_values["evidence_allowlist"],
                    "e:context",
                    "e:relation",
                )
            )
        )
        skeleton_values["source_record_allowlist"] = tuple(
            sorted(
                (
                    *skeleton_values["source_record_allowlist"],
                    "s:context",
                    "s:relation",
                )
            )
        )
    if conversion_first:
        skeleton_values["evidence_allowlist"] = tuple(
            sorted((*skeleton_values["evidence_allowlist"], "e:conversion"))
        )
        skeleton_values["source_record_allowlist"] = tuple(
            sorted((*skeleton_values["source_record_allowlist"], "s:conversion"))
        )
    skeleton = ResultSkeleton(
        **skeleton_values,
        skeleton_digest=canonical_json_digest(skeleton_values),
    )
    plan = _planner(max_cells=max_cells).plan(
        skeleton,
        working_context=_working_context(skeleton),
        model_fit=_model_fit(),
        deadline=DEADLINE,
        batch_response_schema=reasoning_batch_response_schema(),
        synthesis_response_schema=overall_synthesis_response_schema(),
    )
    windows = {window.window_id: window for window in skeleton.windows}
    facts = [
        EvidenceValidationFact(
            evidence_ref=evidence_ref,
            item_id=f"item:{evidence_ref}",
            target_comid=skeleton.target_comid,
            time_range=TimeRange(
                start=windows[scope.window_id].start_time,
                end=windows[scope.window_id].end_time,
            ),
            parameter_refs=(scope.parameter,),
            applicability=EvidenceApplicability.DIRECT_MEASUREMENT,
            result_eligibility=EvidenceResultEligibility.OBSERVED_OR_INFERRED,
            payload_type="direct_record",
            value="7",
            unit_id="mg/l",
            aggregation_method=None,
            source_record_refs=(evidence_ref.replace("e:", "s:", 1),),
            dependency_evidence_refs=(),
            series_ref=f"series:{scope.cell_id}:{evidence_ref}",
            quality_class=EvidenceQualityClass.VALID,
            quality_flags=(),
            registered_reversible_conversion=False,
        )
        for scope in skeleton.supported_scopes
        for evidence_ref in scope.allowed_evidence_refs
    ]
    if conversion_first:
        base_ref = skeleton.supported_scopes[0].allowed_evidence_refs[0]
        base_fact = next(fact for fact in facts if fact.evidence_ref == base_ref)
        facts = [
            fact.model_copy(
                update={
                    "payload_type": "conversion",
                    "source_record_refs": (),
                    "dependency_evidence_refs": (base_ref,),
                    "series_ref": base_fact.series_ref,
                    "registered_reversible_conversion": True,
                }
            )
            if fact.evidence_ref == "e:conversion"
            else fact
            for fact in facts
        ]
    if attribution_context:
        first_scope = skeleton.supported_scopes[0]
        first_window = windows[first_scope.window_id]
        first_ref = first_scope.allowed_evidence_refs[0]
        common_fact = {
            "target_comid": skeleton.target_comid,
            "time_range": TimeRange(
                start=first_window.start_time,
                end=first_window.end_time,
            ),
            "parameter_refs": (first_scope.parameter,),
            "value": None,
            "unit_id": None,
            "aggregation_method": None,
            "quality_class": EvidenceQualityClass.VALID,
            "quality_flags": (),
            "registered_reversible_conversion": False,
        }
        facts.extend(
            (
                EvidenceValidationFact(
                    **common_fact,
                    evidence_ref="e:relation",
                    item_id="item:e:relation",
                    applicability=EvidenceApplicability.ALLOWED_INDIRECT,
                    result_eligibility=EvidenceResultEligibility.INFERRED_ONLY,
                    payload_type="relation_support",
                    source_record_refs=(),
                    dependency_evidence_refs=(first_ref,),
                    series_ref="series:relation",
                ),
                EvidenceValidationFact(
                    **common_fact,
                    evidence_ref="e:context",
                    item_id="item:e:context",
                    applicability=EvidenceApplicability.CONTEXTUAL_ONLY,
                    result_eligibility=EvidenceResultEligibility.INFERRED_ONLY,
                    payload_type="context",
                    source_record_refs=("s:context",),
                    dependency_evidence_refs=(),
                    series_ref="series:context",
                ),
            )
        )
    facts = tuple(sorted(facts, key=lambda fact: fact.evidence_ref))
    scopes = tuple(
        CellEvidenceValidationScope(
            cell_id=scope.cell_id,
            parameter=scope.parameter,
            time_range=TimeRange(
                start=windows[scope.window_id].start_time,
                end=windows[scope.window_id].end_time,
            ),
            target_direct_refs=tuple(sorted(scope.allowed_evidence_refs)),
            target_indirect_refs=(),
            support_direct_refs=(),
            support_indirect_refs=(),
        )
        for scope in skeleton.supported_scopes
    )
    catalog_values = {
        "run_id": skeleton.run_id,
        "session_id": skeleton.session_id,
        "target_comid": skeleton.target_comid,
        "bundle_digest": skeleton.bundle_digest,
        "rule_plan_digest": skeleton.rule_plan_digest,
        "capability_registry_digest": skeleton.capability_registry_digest,
        "facts": facts,
        "cell_scopes": scopes,
    }
    catalog = ReasoningEvidenceCatalog(
        **catalog_values,
        catalog_digest=canonical_json_digest(catalog_values),
    )
    return skeleton, plan, catalog, DraftValidator(catalog)


def rebuild_catalog(
    catalog: ReasoningEvidenceCatalog,
    *,
    facts=None,
    cell_scopes=None,
) -> ReasoningEvidenceCatalog:
    values = catalog.model_dump(mode="python", exclude={"catalog_digest"})
    if facts is not None:
        values["facts"] = tuple(sorted(facts, key=lambda fact: fact.evidence_ref))
    if cell_scopes is not None:
        values["cell_scopes"] = cell_scopes
    return ReasoningEvidenceCatalog(
        **values,
        catalog_digest=canonical_json_digest(values),
    )


def batch_payload(batch, catalog: ReasoningEvidenceCatalog) -> dict[str, object]:
    facts = {fact.evidence_ref: fact for fact in catalog.facts}
    cells = []
    for planned in batch.cells:
        fact = facts[planned.allowed_evidence_refs[0]]
        cells.append(
            {
                "cell_id": planned.cell_id,
                "outcome_status": "quantified",
                "representative_value": fact.value,
                "target_unit_id": fact.unit_id,
                "source": "observed",
                "trend": "stable",
                "risk_level": "none",
                "risk_rationale": "The cited direct observation supports no current risk signal.",
                "confidence": 0.8,
                "confidence_rationale": "The result uses one current direct record.",
                "aggregation_method": fact.aggregation_method,
                "evidence_refs": [fact.evidence_ref],
                "series_refs": [fact.series_ref],
                "reason_codes": [],
                "quality_flags": [flag.value for flag in fact.quality_flags],
                "explanation": "The result is limited to the cited observation.",
            }
        )
    return {
        "schema_version": 1,
        "decision_type": "reasoning_batch",
        "batch_id": batch.batch_id,
        "scope_digest": batch.scope_digest,
        "cells": cells,
    }


def validated_reasoning(plan, validator: DraftValidator, catalog):
    batches = tuple(
        validator.validate_batch(batch, batch_payload(batch, catalog))
        for batch in plan.batch_plan.batches
    )
    return validator.complete_batches(plan.batch_plan, batches)


def synthesis_payload(
    skeleton: ResultSkeleton,
    plan,
    reasoning: ValidatedReasoningSet,
) -> dict[str, object]:
    cells = {
        cell.cell_id: cell
        for batch in reasoning.batches
        for cell in batch.cells
    }
    fixed = {
        cell.result.cell_id: cell.result for cell in skeleton.fixed_cells
    }
    parameter_cells = {
        parameter: tuple(
            cell_id
            for cell_id in skeleton.cell_order
            if cell_id.startswith(f"cell:{parameter.code_system}:{parameter.code}:")
            and cell_id in cells
        )
        for parameter in skeleton.requested_parameters
    }
    series = []
    for parameter in skeleton.requested_parameters:
        refs = parameter_cells[parameter]
        fixed_refs = tuple(
            cell_id
            for cell_id in skeleton.cell_order
            if cell_id.startswith(f"cell:{parameter.code_system}:{parameter.code}:")
            and cell_id in fixed
        )
        series.append(
            {
                "parameter": parameter.model_dump(mode="json"),
                "confidence": 0.75 if refs else 0.0,
                "confidence_rationale": (
                    "The series uses the cited validated cells."
                    if refs
                    else "No supported cell is available for this series."
                ),
                "contributing_cell_refs": list(refs),
                "evidence_refs": sorted(
                    {
                        ref
                        for cell_id in (*refs, *fixed_refs)
                        for ref in (
                            cells[cell_id].evidence_refs
                            if cell_id in cells
                            else fixed[cell_id].evidence_refs
                        )
                    }
                ),
            }
        )
    windows = []
    for window in skeleton.windows:
        refs = tuple(
            cell_id
            for cell_id in skeleton.cell_order
            if cell_id.endswith(f":{window.window_id}") and cell_id in cells
        )
        fixed_refs = tuple(
            cell_id
            for cell_id in skeleton.cell_order
            if cell_id.endswith(f":{window.window_id}") and cell_id in fixed
        )
        windows.append(
            {
                "window_id": window.window_id,
                "risk_level": "low" if refs else "indeterminate",
                "risk_rationale": (
                    "The validated cells indicate a limited risk signal."
                    if refs
                    else "No supported cell is available for this window."
                ),
                "confidence": 0.7 if refs else 0.0,
                "confidence_rationale": (
                    "The window is backed by cited cell evidence."
                    if refs
                    else "No supported water-quality judgment is available."
                ),
                "contributing_cell_refs": list(refs),
                "evidence_refs": sorted(
                    {
                        ref
                        for cell_id in (*refs, *fixed_refs)
                        for ref in (
                            cells[cell_id].evidence_refs
                            if cell_id in cells
                            else fixed[cell_id].evidence_refs
                        )
                    }
                ),
            }
        )
    all_cells = tuple(cells)
    all_evidence = sorted(
        {
            ref
            for cell in (*cells.values(), *fixed.values())
            for ref in cell.evidence_refs
        }
    )
    first_cell = all_cells[0]
    first_evidence = cells[first_cell].evidence_refs[0]
    return {
        "schema_version": 1,
        "decision_type": "overall_synthesis",
        "scope_digest": plan.synthesis_plan.scope_digest,
        "series_assessments": series,
        "window_assessments": windows,
        "overall": {
            "risk_level": "low",
            "risk_rationale": "The complete requested range has a limited risk signal.",
            "confidence": 0.7,
            "confidence_rationale": "All cited windows and cells were considered.",
            "contributing_window_refs": [
                f"overall-window:{window.window_id}" for window in skeleton.windows
            ],
            "contributing_cell_refs": list(all_cells),
            "evidence_refs": all_evidence,
        },
        "analysis_detail": {
            "summary": "The assessment follows the validated cell and window evidence.",
            "steps": [
                {
                    "type": "evidence_review",
                    "title": "Review validated evidence",
                    "content": "The cited observation was reviewed within its scope.",
                    "evidence_refs": [first_evidence],
                    "result_cell_refs": [first_cell],
                }
            ],
        },
        "attribution_summary": {
            "summary": "The direct observation is the primary factor.",
            "primary_factors": [
                {
                    "factor": "Current direct observation",
                    "direction": "neutral",
                    "magnitude": "low",
                    "basis": AttributionBasis.OBSERVED.value,
                    "explanation": "The factor is limited to one validated observed cell.",
                    "evidence_refs": [first_evidence],
                    "result_cell_refs": [first_cell],
                }
            ],
        },
    }
