from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from typing import Literal, Self
from uuid import UUID

from pydantic import (
    StrictBool,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from app.application.evidence_tools.measurement_provenance import (
    derive_measured_anchors,
)
from app.application.guidelines.citation_resolver import sanitize_guideline_refs
from app.application.reasoning.batch_planner import (
    ReasoningBatch,
    ReasoningBatchPlan,
    SynthesisInputPlan,
)
from app.application.reasoning.draft_models import (
    DraftValidationIssue,
    OverallSynthesisDraft,
    ReasoningBatchDraft,
    ReasoningCellDraft,
    ValidatedOverallSynthesis,
    ValidatedReasoningBatch,
    ValidatedReasoningSet,
)
from app.application.reasoning.errors import DraftValidationError
from app.application.reasoning.prompt_policy import canonical_json_digest
from app.application.reasoning.result_models import ResultSkeleton
from app.application.reasoning.sufficiency import (
    SealedEvidenceInput,
    validate_sealed_evidence_input,
)
from app.domain import (
    AggregatePayload,
    AggregationMethod,
    AttributionBasis,
    ConversionPayload,
    DataQualityFlag,
    DirectRecordPayload,
    DomainFloat,
    DomainModel,
    EvidenceApplicability,
    EvidenceCellRole,
    EvidenceFragment,
    EvidenceQualityClass,
    EvidenceResultEligibility,
    IndeterminateReason,
    InferenceMode,
    NonEmptyString,
    NumericDerivedPayload,
    OutcomeStatus,
    ParameterKey,
    RelationSupportPayload,
    ResultSource,
    RiskLevel,
    TemporalEstimatePayload,
    TimeRange,
    ensure_unique_tuple,
)
from app.domain.evidence.measured_anchor import MeasuredAnchor

logger = logging.getLogger("wateragents.reasoning")


class EvidenceValidationFact(DomainModel):
    evidence_ref: NonEmptyString
    item_id: NonEmptyString
    target_comid: NonEmptyString
    time_range: TimeRange
    parameter_refs: tuple[ParameterKey, ...]
    applicability: EvidenceApplicability
    result_eligibility: EvidenceResultEligibility
    payload_type: NonEmptyString
    value: NonEmptyString | None
    value_lower: NonEmptyString | None = None
    value_upper: NonEmptyString | None = None
    method_kind: Literal["explicit_calculation", "model_estimate"] | None = None
    inference_basis: Literal["same_parameter_measurement", "related_evidence", "general_knowledge"] | None = None
    measured_anchors: tuple[MeasuredAnchor, ...] = ()
    unit_id: NonEmptyString | None
    aggregation_method: AggregationMethod | None
    source_record_refs: tuple[NonEmptyString, ...]
    dependency_evidence_refs: tuple[NonEmptyString, ...]
    series_ref: NonEmptyString | None
    quality_class: EvidenceQualityClass | None
    quality_flags: tuple[DataQualityFlag, ...]
    registered_reversible_conversion: StrictBool
    location_comid: NonEmptyString | None = None
    station_id: NonEmptyString | None = None
    temporal_method: NonEmptyString | None = None
    anchor_distance_seconds: tuple[StrictInt, ...] = ()
    numeric_method_id: NonEmptyString | None = None
    numeric_method_version: StrictInt | None = None
    numeric_task_id: NonEmptyString | None = None
    numeric_plan_digest: NonEmptyString | None = None
    numeric_plan_item_id: NonEmptyString | None = None
    numeric_method_registry_digest: NonEmptyString | None = None
    numeric_confidence_cap: DomainFloat | None = None
    numeric_input_bindings: tuple[
        tuple[NonEmptyString, NonEmptyString, NonEmptyString], ...
    ] = ()

    @field_validator(
        "source_record_refs",
        "dependency_evidence_refs",
        "quality_flags",
    )
    @classmethod
    def canonicalize_refs(cls, value: tuple[object, ...]) -> tuple[object, ...]:
        ensure_unique_tuple(value, field_name="evidence validation facts")
        return tuple(sorted(value, key=lambda item: getattr(item, "value", item)))


class CellEvidenceValidationScope(DomainModel):
    cell_id: NonEmptyString
    parameter: ParameterKey
    time_range: TimeRange
    target_direct_refs: tuple[NonEmptyString, ...]
    target_indirect_refs: tuple[NonEmptyString, ...]
    support_direct_refs: tuple[NonEmptyString, ...]
    support_indirect_refs: tuple[NonEmptyString, ...]

    @model_validator(mode="after")
    def validate_refs(self) -> Self:
        groups = (
            self.target_direct_refs,
            self.target_indirect_refs,
            self.support_direct_refs,
            self.support_indirect_refs,
        )
        for values in groups:
            ensure_unique_tuple(values, field_name="cell evidence scope")
        flattened = tuple(ref for values in groups for ref in values)
        ensure_unique_tuple(flattened, field_name="cell evidence scope closure")
        return self

    @property
    def allowed_refs(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                (
                    *self.target_direct_refs,
                    *self.target_indirect_refs,
                    *self.support_direct_refs,
                    *self.support_indirect_refs,
                )
            )
        )


class ReasoningEvidenceCatalog(DomainModel):
    run_id: UUID
    session_id: UUID
    target_comid: NonEmptyString
    bundle_digest: NonEmptyString
    rule_plan_digest: NonEmptyString
    capability_registry_digest: NonEmptyString
    facts: tuple[EvidenceValidationFact, ...]
    cell_scopes: tuple[CellEvidenceValidationScope, ...]
    catalog_digest: NonEmptyString

    @model_validator(mode="after")
    def validate_catalog(self) -> Self:
        if tuple(fact.evidence_ref for fact in self.facts) != tuple(
            sorted(fact.evidence_ref for fact in self.facts)
        ):
            raise ValueError("evidence validation facts must use canonical order")
        ensure_unique_tuple(
            tuple(fact.evidence_ref for fact in self.facts),
            field_name="evidence validation fact IDs",
        )
        ensure_unique_tuple(
            tuple(scope.cell_id for scope in self.cell_scopes),
            field_name="evidence validation cell IDs",
        )
        known = {fact.evidence_ref for fact in self.facts}
        if any(fact.target_comid != self.target_comid for fact in self.facts):
            raise ValueError("evidence validation fact target mismatch")
        if any(
            not set(fact.dependency_evidence_refs) <= known
            for fact in self.facts
        ):
            raise ValueError("evidence validation dependency is unknown")
        if any(
            not set(scope.allowed_refs) <= known for scope in self.cell_scopes
        ):
            raise ValueError("cell evidence scope references unknown evidence")
        facts = {fact.evidence_ref: fact for fact in self.facts}
        for fact in self.facts:
            if fact.payload_type == "temporal_estimate":
                _validate_temporal_estimate_fact(fact, facts)
            elif fact.payload_type == "numeric_derived":
                _validate_numeric_derived_fact(fact, facts)
        for scope in self.cell_scopes:
            for refs, expected_applicability in (
                (
                    (*scope.target_direct_refs, *scope.support_direct_refs),
                    EvidenceApplicability.DIRECT_MEASUREMENT,
                ),
                (
                    (*scope.target_indirect_refs, *scope.support_indirect_refs),
                    EvidenceApplicability.ALLOWED_INDIRECT,
                ),
            ):
                if any(
                    facts[ref].applicability is not expected_applicability
                    for ref in refs
                ):
                    raise ValueError(
                        "cell evidence directness must match the sealed scope"
                    )
            if any(
                facts[ref].time_range.start >= scope.time_range.end
                or facts[ref].time_range.end <= scope.time_range.start
                for ref in scope.allowed_refs
            ):
                raise ValueError("cell evidence scope must overlap its window")
            for evidence_ref in (
                *scope.target_direct_refs,
                *scope.target_indirect_refs,
            ):
                fact = facts[evidence_ref]
                if (
                    scope.parameter not in fact.parameter_refs
                    or fact.time_range.start >= scope.time_range.end
                    or fact.time_range.end <= scope.time_range.start
                ):
                    raise ValueError(
                        "target evidence scope must match parameter and window"
                    )
        expected = canonical_json_digest(
            self.model_dump(mode="json", exclude={"catalog_digest"})
        )
        if self.catalog_digest != expected:
            raise ValueError("evidence validation catalog digest mismatch")
        return self


def build_reasoning_evidence_catalog(
    sealed_input: SealedEvidenceInput,
    fragments: tuple[EvidenceFragment, ...],
) -> ReasoningEvidenceCatalog:
    sealed = validate_sealed_evidence_input(sealed_input)
    by_id = {fragment.evidence_id: fragment for fragment in fragments}
    if len(by_id) != len(fragments):
        raise ValueError("evidence validation fragments must be unique")
    reference_by_id = {
        reference.evidence_id: reference for reference in sealed.fragment_allowlist
    }
    if set(by_id) != set(reference_by_id):
        raise ValueError("evidence validation fragments must match sealed allowlist")
    if any(fragment.target_comid != sealed.target_comid for fragment in fragments):
        raise ValueError("evidence validation fragment target mismatch")
    if any(
        not {
            reference.item_id for reference in fragment.source_record_refs
        } <= set(sealed.source_record_allowlist)
        for fragment in fragments
    ):
        raise ValueError("evidence validation source record is outside sealed scope")
    window_ranges = {
        window.window_id: window.time_range for window in sealed.windows
    }
    for fragment in fragments:
        if isinstance(fragment.payload, TemporalEstimatePayload):
            plan_ref = fragment.payload.plan_item_ref
            if (
                plan_ref is not None
                and plan_ref.item_id not in sealed.numeric_plan_item_refs
            ):
                raise ValueError("temporal derivation plan is outside sealed closure")
        if not isinstance(fragment.payload, NumericDerivedPayload):
            continue
        payload = fragment.payload
        if payload.plan_item_ref.item_id not in sealed.numeric_plan_item_refs:
            raise ValueError("numeric derivation plan is outside sealed closure")
        for reference, window_id in zip(
            payload.input_evidence_refs, payload.input_window_ids, strict=True
        ):
            input_fragment = by_id.get(reference.evidence_id)
            if (
                reference_by_id.get(reference.evidence_id) != reference
                or input_fragment is None
                or (window_id == "context" and payload.method_kind != "model_estimate")
                or (window_id != "context" and (
                    window_id not in window_ranges
                    or not input_fragment.time_range.overlaps(window_ranges[window_id])
                ))
            ):
                raise ValueError("numeric derivation input is outside sealed scope")

    facts = tuple(
        sorted(
            (
                _evidence_fact(
                    reference_by_id[fragment.evidence_id].item_id,
                    fragment,
                    capability_registry_digest=sealed.capability_registry_digest,
                    measured_anchors=derive_measured_anchors(
                        parameter=fragment.parameter_refs[0],
                        target_comid=sealed.target_comid,
                        evidence_ids=(fragment.evidence_id,), fragments=by_id,
                    ) if len(fragment.parameter_refs) == 1 else (),
                )
                for fragment in fragments
            ),
            key=lambda fact: fact.evidence_ref,
        )
    )
    cells = {
        (cell.parameter, cell.window_id, cell.role): cell for cell in sealed.cells
    }
    discharge = ParameterKey(code_system="usgs", code="00060")
    discharge_role = (
        EvidenceCellRole.TARGET
        if discharge in sealed.requested_parameters else EvidenceCellRole.REQUIRED_SUPPORT
    )
    scopes = []
    windows = {window.window_id: window for window in sealed.windows}
    for gate in sealed.gates:
        target = cells[
            (gate.parameter, gate.window_id, EvidenceCellRole.TARGET)
        ]
        support = cells[
            (discharge, gate.window_id, discharge_role)
        ]
        target_direct = tuple(sorted(target.direct_fragment_refs))
        target_indirect = tuple(sorted(target.allowed_indirect_fragment_refs))
        scope = CellEvidenceValidationScope(
            cell_id=gate.cell_id,
            parameter=gate.parameter,
            time_range=windows[gate.window_id].time_range,
            target_direct_refs=target_direct,
            target_indirect_refs=target_indirect,
            support_direct_refs=tuple(sorted(
                set(support.direct_fragment_refs) - set(target_direct)
            )),
            support_indirect_refs=tuple(
                sorted(set(support.allowed_indirect_fragment_refs) - set(target_indirect))
            ),
        )
        if scope.allowed_refs != tuple(sorted(gate.allowed_evidence_refs)):
            raise ValueError("cell evidence scope does not match sealed gate")
        scopes.append(scope)
    values = {
        "run_id": sealed.run_id,
        "session_id": sealed.session_id,
        "target_comid": sealed.target_comid,
        "bundle_digest": sealed.bundle_digest,
        "rule_plan_digest": sealed.rule_plan_digest,
        "capability_registry_digest": sealed.capability_registry_digest,
        "facts": facts,
        "cell_scopes": tuple(scopes),
    }
    return ReasoningEvidenceCatalog(
        **values,
        catalog_digest=canonical_json_digest(values),
    )


class DraftValidator:
    def __init__(self, evidence_catalog: ReasoningEvidenceCatalog) -> None:
        self._catalog = evidence_catalog
        self._facts = {fact.evidence_ref: fact for fact in evidence_catalog.facts}
        self._cell_scopes = {
            scope.cell_id: scope for scope in evidence_catalog.cell_scopes
        }

    def validate_batch(
        self,
        batch: ReasoningBatch,
        raw: object,
        *, guideline_aliases=frozenset(),
    ) -> ValidatedReasoningBatch:
        self._validate_batch_identity(batch)
        normalized, filled_unit_paths = _fill_single_allowed_units(raw, batch)
        normalized, filled_range_paths = _fill_cited_numeric_ranges(
            normalized, batch, self._facts, self._cell_scopes
        )
        draft = _parse_draft(normalized, ReasoningBatchDraft, guideline_aliases=guideline_aliases)
        issues: list[DraftValidationIssue] = []
        if draft.batch_id != batch.batch_id:
            _issue(issues, "batch_identity_mismatch", "$.batch_id")
        if draft.scope_digest != batch.scope_digest:
            _issue(issues, "scope_digest_mismatch", "$.scope_digest")
        expected_ids = tuple(cell.cell_id for cell in batch.cells)
        actual_ids = tuple(cell.cell_id for cell in draft.cells)
        if len(actual_ids) != len(set(actual_ids)):
            _issue(issues, "duplicate_cell", "$.cells")
        if set(expected_ids) - set(actual_ids):
            _issue(issues, "missing_cell", "$.cells")
        if set(actual_ids) - set(expected_ids):
            _issue(issues, "extra_cell", "$.cells")
        if set(actual_ids) == set(expected_ids) and actual_ids != expected_ids:
            _issue(issues, "cell_order_mismatch", "$.cells")

        scopes = {cell.cell_id: cell for cell in batch.cells}
        normalized_cells: list[ReasoningCellDraft] = []
        normalized_flag_paths: list[str] = []
        for index, cell in enumerate(draft.cells):
            planned = scopes.get(cell.cell_id)
            if planned is not None:
                if set(cell.evidence_refs) <= set(planned.allowed_evidence_refs):
                    expected_flags = _quality_flags_from_facts(
                        self._facts[ref] for ref in cell.evidence_refs
                    )
                    if cell.quality_flags != expected_flags:
                        cell = cell.model_copy(
                            update={"quality_flags": expected_flags}
                        )
                        normalized_flag_paths.append(
                            f"$.cells[{index}].quality_flags"
                        )
                self._validate_cell(
                    cell,
                    planned=planned,
                    path=f"$.cells[{index}]",
                    issues=issues,
                )
            normalized_cells.append(cell)
        _raise_issues(issues)
        draft = draft.model_copy(update={"cells": tuple(normalized_cells)})
        for path in filled_unit_paths:
            logger.info(
                "Reasoning draft unit filled from its single allowed unit.",
                extra={
                    "event_name": "reasoning_draft_single_unit_filled",
                    "run_id": batch.run_id,
                    "session_id": batch.session_id,
                    "operation": batch.operation_id,
                    "schema_path": path,
                    "schema_rule": "single_allowed_unit",
                },
            )
        for path in filled_range_paths:
            logger.info(
                "Reasoning draft range filled from its uniquely cited numeric evidence.",
                extra={
                    "event_name": "reasoning_draft_numeric_range_filled",
                    "run_id": batch.run_id,
                    "session_id": batch.session_id,
                    "operation": batch.operation_id,
                    "batch_id": batch.batch_id,
                    "schema_path": path,
                    "schema_rule": "unique_cited_numeric_derived_range",
                },
            )
        for path in normalized_flag_paths:
            logger.info(
                "Reasoning draft quality flags normalized from cited evidence.",
                extra={
                    "event_name": "reasoning_draft_quality_flags_normalized",
                    "run_id": batch.run_id,
                    "session_id": batch.session_id,
                    "operation": batch.operation_id,
                    "schema_path": path,
                    "schema_rule": "cited_evidence_union",
                },
            )
        values = {
            "batch_id": batch.batch_id,
            "scope_digest": batch.scope_digest,
            "cells": draft.cells,
        }
        return ValidatedReasoningBatch(
            **values,
            validation_digest=canonical_json_digest(values),
        )

    def complete_batches(
        self,
        plan: ReasoningBatchPlan,
        batches: tuple[ValidatedReasoningBatch, ...],
    ) -> ValidatedReasoningSet:
        issues: list[DraftValidationIssue] = []
        expected_ids = tuple(batch.batch_id for batch in plan.batches)
        actual_ids = tuple(batch.batch_id for batch in batches)
        if len(actual_ids) != len(set(actual_ids)):
            _issue(issues, "duplicate_batch", "$.batches")
        if set(expected_ids) - set(actual_ids):
            _issue(issues, "missing_batch", "$.batches")
        if set(actual_ids) - set(expected_ids):
            _issue(issues, "extra_batch", "$.batches")
        if set(actual_ids) == set(expected_ids) and actual_ids != expected_ids:
            _issue(issues, "batch_order_mismatch", "$.batches")
        expected_cells = plan.supported_cell_order
        actual_cells = tuple(
            cell.cell_id for batch in batches for cell in batch.cells
        )
        if actual_cells != expected_cells:
            _issue(issues, "validated_cells_incomplete", "$.batches")
        for index, (planned, validated) in enumerate(zip(plan.batches, batches)):
            if validated.scope_digest != planned.scope_digest:
                _issue(
                    issues,
                    "scope_digest_mismatch",
                    f"$.batches[{index}].scope_digest",
                )
        _raise_issues(issues)
        values = {
            "batch_plan_digest": plan.plan_digest,
            "batches": batches,
        }
        return ValidatedReasoningSet(
            **values,
            validation_digest=canonical_json_digest(values),
        )

    def validate_synthesis(
        self,
        skeleton: ResultSkeleton,
        batch_plan: ReasoningBatchPlan,
        plan: SynthesisInputPlan,
        reasoning: ValidatedReasoningSet,
        raw: object,
        *, guideline_aliases=frozenset(),
    ) -> ValidatedOverallSynthesis:
        draft = _parse_draft(raw, OverallSynthesisDraft, guideline_aliases=guideline_aliases)
        issues: list[DraftValidationIssue] = []
        if (
            reasoning.batch_plan_digest != batch_plan.plan_digest
            or batch_plan.run_id != plan.run_id
            or batch_plan.session_id != plan.session_id
            or batch_plan.result_id != plan.result_id
            or batch_plan.scope_digest != plan.scope_digest
            or batch_plan.supported_cell_order != plan.validated_cell_order
        ):
            _issue(issues, "reasoning_plan_mismatch", "$.reasoning")
        if draft.scope_digest != plan.scope_digest:
            _issue(issues, "scope_digest_mismatch", "$.scope_digest")
        validated_cells = {
            cell.cell_id: cell
            for batch in reasoning.batches
            for cell in batch.cells
        }
        if tuple(validated_cells) != plan.validated_cell_order:
            _issue(issues, "validated_cells_incomplete", "$.reasoning")
        expected_parameters = skeleton.requested_parameters
        actual_parameters = tuple(
            item.parameter for item in draft.series_assessments
        )
        if actual_parameters != expected_parameters:
            _issue(issues, "series_order_mismatch", "$.series_assessments")
        expected_windows = tuple(window.window_id for window in skeleton.windows)
        actual_windows = tuple(item.window_id for item in draft.window_assessments)
        if actual_windows != expected_windows:
            _issue(issues, "window_order_mismatch", "$.window_assessments")

        fixed_by_id = {
            cell.result.cell_id: cell for cell in skeleton.fixed_cells
        }
        assessed_cells = {
            cell_id
            for cell_id, cell in validated_cells.items()
            if cell.outcome_status is not OutcomeStatus.INDETERMINATE
        }
        noncontributing_evidence = {
            **{
                cell_id: fixed.result.evidence_refs
                for cell_id, fixed in fixed_by_id.items()
            },
            **{
                cell_id: cell.evidence_refs
                for cell_id, cell in validated_cells.items()
                if cell_id not in assessed_cells
            },
        }
        all_cell_ids = set(skeleton.cell_order)
        known_evidence = set(skeleton.evidence_allowlist)
        for index, series in enumerate(draft.series_assessments):
            if series.parameter not in expected_parameters:
                continue
            parameter_cells = {
                cell_id
                for cell_id in skeleton.cell_order
                if cell_id.startswith(
                    f"cell:{series.parameter.code_system}:{series.parameter.code}:"
                )
            }
            allowed_cells = parameter_cells & assessed_cells
            self._validate_synthesis_refs(
                series.contributing_cell_refs,
                series.evidence_refs,
                allowed_cells=allowed_cells,
                noncontributing_cells=parameter_cells & set(noncontributing_evidence),
                validated_cells=validated_cells,
                noncontributing_evidence=noncontributing_evidence,
                known_evidence=known_evidence,
                path=f"$.series_assessments[{index}]",
                issues=issues,
            )
            if not allowed_cells and series.confidence != 0:
                _issue(
                    issues,
                    "fixed_series_must_have_zero_confidence",
                    f"$.series_assessments[{index}].confidence",
                )
        windows_by_id = {window.window_id: window for window in skeleton.windows}
        for index, window in enumerate(draft.window_assessments):
            if window.window_id not in windows_by_id:
                continue
            suffix = f":{window.window_id}"
            window_cells = {
                cell_id for cell_id in skeleton.cell_order if cell_id.endswith(suffix)
            }
            allowed_cells = window_cells & assessed_cells
            self._validate_synthesis_refs(
                window.contributing_cell_refs,
                window.evidence_refs,
                allowed_cells=allowed_cells,
                noncontributing_cells=window_cells & set(noncontributing_evidence),
                validated_cells=validated_cells,
                noncontributing_evidence=noncontributing_evidence,
                known_evidence=known_evidence,
                path=f"$.window_assessments[{index}]",
                issues=issues,
            )
            if not allowed_cells and (
                window.risk_level is not RiskLevel.INDETERMINATE
                or window.confidence != 0
            ):
                _issue(
                    issues,
                    "fixed_window_must_be_indeterminate",
                    f"$.window_assessments[{index}]",
                )

        known_window_refs = {
            f"overall-window:{window_id}" for window_id in expected_windows
        }
        if set(draft.overall.contributing_window_refs) != known_window_refs:
            _issue(
                issues,
                "window_references_incomplete",
                "$.overall.contributing_window_refs",
            )
        self._validate_synthesis_refs(
            draft.overall.contributing_cell_refs,
            draft.overall.evidence_refs,
            allowed_cells=assessed_cells,
            noncontributing_cells=set(noncontributing_evidence),
            validated_cells=validated_cells,
            noncontributing_evidence=noncontributing_evidence,
            known_evidence=known_evidence,
            path="$.overall",
            issues=issues,
        )
        if draft.overall.risk_level is RiskLevel.NONE and (
            not draft.overall.contributing_cell_refs
            or not draft.overall.evidence_refs
        ):
            _issue(issues, "none_risk_without_evidence", "$.overall.risk_level")
        if not assessed_cells and (
            draft.overall.risk_level is not RiskLevel.INDETERMINATE
            or draft.overall.confidence != 0
        ):
            _issue(
                issues,
                "indeterminate_overall_required",
                "$.overall",
            )

        for index, step in enumerate(draft.analysis_detail.steps):
            self._validate_open_refs(
                step.evidence_refs,
                step.result_cell_refs,
                known_evidence=known_evidence,
                known_cells=all_cell_ids,
                path=f"$.analysis_detail.steps[{index}]",
                issues=issues,
            )
        for index, factor in enumerate(
            draft.attribution_summary.primary_factors
        ):
            path = f"$.attribution_summary.primary_factors[{index}]"
            self._validate_open_refs(
                factor.evidence_refs,
                factor.result_cell_refs,
                known_evidence=known_evidence,
                known_cells=all_cell_ids,
                path=path,
                issues=issues,
            )
            self._validate_attribution_basis(
                factor.basis,
                factor.evidence_refs,
                factor.result_cell_refs,
                validated_cells=validated_cells,
                path=path,
                issues=issues,
            )
        _raise_issues(issues)
        values = {
            "scope_digest": plan.scope_digest,
            "series_assessments": draft.series_assessments,
            "window_assessments": draft.window_assessments,
            "overall": draft.overall,
            "analysis_detail": draft.analysis_detail,
            "attribution_summary": draft.attribution_summary,
        }
        return ValidatedOverallSynthesis(
            **values,
            validation_digest=canonical_json_digest(values),
        )

    def _validate_batch_identity(self, batch: ReasoningBatch) -> None:
        catalog = self._catalog
        catalog_scopes = self._cell_scopes
        if (
            batch.run_id != catalog.run_id
            or batch.session_id != catalog.session_id
            or batch.context.target_comid != catalog.target_comid
            or batch.bundle_digest != catalog.bundle_digest
            or batch.rule_plan_digest != catalog.rule_plan_digest
            or batch.capability_registry_digest
            != catalog.capability_registry_digest
            or any(
                cell.cell_id not in catalog_scopes
                or cell.parameter != catalog_scopes[cell.cell_id].parameter
                or cell.start_time != catalog_scopes[cell.cell_id].time_range.start
                or cell.end_time != catalog_scopes[cell.cell_id].time_range.end
                or tuple(sorted(cell.allowed_evidence_refs))
                != catalog_scopes[cell.cell_id].allowed_refs
                for cell in batch.cells
            )
        ):
            raise DraftValidationError(
                (DraftValidationIssue(code="batch_catalog_mismatch", path="$"),)
            )

    def _validate_cell(
        self,
        cell: ReasoningCellDraft,
        *,
        planned,
        path: str,
        issues: list[DraftValidationIssue],
    ) -> None:
        evidence_scope = self._cell_scopes[cell.cell_id]
        refs = set(cell.evidence_refs)
        for field_name in ("risk_rationale", "confidence_rationale", "explanation"):
            value = getattr(cell, field_name)
            if any(
                ref not in refs
                and re.search(
                    rf"(?<![A-Za-z0-9_:#-]){re.escape(ref)}(?![A-Za-z0-9_:#-])",
                    value,
                )
                for ref in self._facts
            ):
                _issue(issues, "text_evidence_ref_missing", f"{path}.{field_name}")
        if not refs <= set(planned.allowed_evidence_refs):
            _issue(issues, "evidence_out_of_scope", f"{path}.evidence_refs")
            return
        facts = tuple(self._facts[ref] for ref in cell.evidence_refs)
        dependencies = {
            dependency
            for fact in facts
            if fact.payload_type not in {
                "relation_support", "temporal_estimate", "numeric_derived"
            }
            for dependency in fact.dependency_evidence_refs
        }
        if not dependencies <= refs:
            _issue(
                issues,
                "reference_closure_incomplete",
                f"{path}.evidence_refs",
            )
        expected_series = tuple(
            sorted({fact.series_ref for fact in facts if fact.series_ref is not None})
        )
        if cell.series_refs != expected_series:
            _issue(issues, "series_refs_incomplete", f"{path}.series_refs")
        expected_flags = _quality_flags_from_facts(facts)
        if cell.quality_flags != expected_flags:
            _issue(issues, "quality_flags_mismatch", f"{path}.quality_flags")
        selected_direct = any(
            fact.evidence_ref in evidence_scope.target_direct_refs
            and fact.value == cell.representative_value
            and fact.value_lower == cell.value_lower
            and fact.value_upper == cell.value_upper
            and fact.unit_id == cell.target_unit_id
            and fact.aggregation_method is cell.aggregation_method
            for fact in facts
        )
        selected_derived = tuple(
            fact for fact in facts
            if not selected_direct
            and fact.evidence_ref in evidence_scope.target_indirect_refs
            and fact.value == cell.representative_value
            and fact.value_lower == cell.value_lower
            and fact.value_upper == cell.value_upper
            and fact.unit_id == cell.target_unit_id
            and fact.aggregation_method is cell.aggregation_method
        )
        temporal_facts = tuple(
            fact for fact in selected_derived
            if fact.payload_type == "temporal_estimate"
        )
        numeric_facts = tuple(
            fact for fact in selected_derived
            if fact.payload_type == "numeric_derived"
        )
        support_refs = (
            *evidence_scope.support_direct_refs,
            *evidence_scope.support_indirect_refs,
        )
        if support_refs and all(
            self._facts[ref].payload_type
            in {"temporal_estimate", "numeric_derived"}
            for ref in support_refs
        ):
            cited_support_refs = refs & set(support_refs)
            if (
                cell.outcome_status is not OutcomeStatus.INDETERMINATE
                and not cited_support_refs
            ):
                _issue(
                    issues,
                    (
                        "temporal_discharge_support_missing"
                        if all(
                            self._facts[ref].payload_type == "temporal_estimate"
                            for ref in support_refs
                        )
                        else "derived_discharge_support_missing"
                    ),
                    f"{path}.evidence_refs",
                )
        if temporal_facts:
            confidence_cap = min(
                _temporal_confidence_cap(fact, evidence_scope.time_range)
                for fact in temporal_facts
            )
            if cell.confidence > confidence_cap:
                _issue(issues, "temporal_confidence_too_high", f"{path}.confidence")
        numeric_caps = tuple(fact.numeric_confidence_cap for fact in numeric_facts)
        if numeric_caps and (
            None in numeric_caps
            or cell.confidence > min(cap for cap in numeric_caps if cap is not None)
        ):
            _issue(issues, "numeric_confidence_too_high", f"{path}.confidence")
        if any(
            fact.quality_class is EvidenceQualityClass.INVALID
            or fact.result_eligibility is EvidenceResultEligibility.EXPLANATORY_ONLY
            for fact in facts
        ):
            _issue(issues, "ineligible_evidence", f"{path}.evidence_refs")
        if cell.source is not None and cell.source not in planned.allowed_result_sources:
            _issue(issues, "source_not_allowed", f"{path}.source")
        if planned.mode is InferenceMode.PREDICTION:
            if (
                cell.outcome_status is not OutcomeStatus.INDETERMINATE
                and cell.source is not ResultSource.PREDICTED
            ):
                _issue(issues, "prediction_source_required", f"{path}.source")
        elif cell.source is ResultSource.PREDICTED:
            _issue(issues, "prediction_source_not_allowed", f"{path}.source")

        if cell.target_unit_id is not None and (
            cell.target_unit_id not in planned.allowed_unit_ids
        ):
            _issue(issues, "unit_not_allowed", f"{path}.target_unit_id")
        if cell.aggregation_method is not None and (
            cell.aggregation_method not in planned.allowed_aggregation_methods
        ):
            _issue(
                issues,
                "aggregation_not_allowed",
                f"{path}.aggregation_method",
            )
        target_direct = refs & set(evidence_scope.target_direct_refs)
        target_indirect = refs & set(evidence_scope.target_indirect_refs)
        target_estimates = {
            ref
            for ref in target_indirect
            if self._facts[ref].payload_type == "temporal_estimate"
        }
        target_derived = {
            ref
            for ref in target_indirect
            if self._facts[ref].payload_type == "numeric_derived"
        }
        selected_direct = target_direct
        if cell.outcome_status in {OutcomeStatus.QUANTIFIED, OutcomeStatus.RANGE_ESTIMATE}:
            if not target_direct and not target_estimates and not target_derived:
                _issue(issues, "only_indirect_quantified", f"{path}.outcome_status")
            candidates = tuple(
                fact
                for fact in facts
                if fact.evidence_ref in target_direct | target_estimates | target_derived
                and fact.value == cell.representative_value
                and fact.value_lower == cell.value_lower
                and fact.value_upper == cell.value_upper
                and fact.unit_id == cell.target_unit_id
            )
            if not candidates:
                _issue(
                    issues,
                    "quantified_value_not_backed",
                    f"{path}.representative_value",
                )
            matching_method = tuple(
                fact
                for fact in candidates
                if fact.aggregation_method is cell.aggregation_method
            )
            if candidates and not matching_method:
                _issue(
                    issues,
                    "aggregation_mismatch",
                    f"{path}.aggregation_method",
                )
            selected_direct = {
                fact.evidence_ref
                for fact in matching_method
                if fact.evidence_ref in target_direct
            }
            if (
                not selected_direct
                and target_estimates
                and any(
                    fact.evidence_ref in target_estimates for fact in matching_method
                )
                and cell.source is not (
                    ResultSource.PREDICTED if planned.mode is InferenceMode.PREDICTION
                    else ResultSource.INFERRED
                )
            ):
                _issue(issues, "temporal_estimate_requires_inferred", f"{path}.source")
            if (
                not selected_direct
                and target_derived
                and any(
                    fact.evidence_ref in target_derived for fact in matching_method
                )
                and cell.source is not (
                    ResultSource.PREDICTED if planned.mode is InferenceMode.PREDICTION
                    else ResultSource.INFERRED
                )
            ):
                _issue(issues, "numeric_derivation_requires_inferred", f"{path}.source")
        elif (
            cell.outcome_status is OutcomeStatus.DIRECTIONAL
            and not target_direct
            and not target_indirect
        ):
            _issue(issues, "directional_target_evidence_missing", f"{path}.evidence_refs")
        if cell.source is ResultSource.OBSERVED:
            self._validate_observed(
                facts,
                target_direct=selected_direct,
                path=path,
                issues=issues,
            )
        allowed_indeterminate_reasons = {
            IndeterminateReason.INSUFFICIENT_EVIDENCE,
            IndeterminateReason.INVALID_OBSERVATIONS,
            IndeterminateReason.NO_APPLICABLE_EVIDENCE,
        }
        if (
            cell.outcome_status is OutcomeStatus.INDETERMINATE
            and not set(cell.reason_codes) <= allowed_indeterminate_reasons
        ):
            _issue(
                issues,
                "indeterminate_reason_not_allowed",
                f"{path}.reason_codes",
            )

    def _validate_observed(
        self,
        facts: tuple[EvidenceValidationFact, ...],
        *,
        target_direct: set[str],
        path: str,
        issues: list[DraftValidationIssue],
    ) -> None:
        target_facts = tuple(
            fact for fact in facts if fact.evidence_ref in target_direct
        )
        closure = _fact_closure(target_facts, self._facts)
        source_records = {
            source_ref for fact in closure for source_ref in fact.source_record_refs
        }
        series = {fact.series_ref for fact in target_facts if fact.series_ref}
        depended_on = {
            dependency
            for fact in target_facts
            for dependency in fact.dependency_evidence_refs
        }
        terminal_target_facts = {
            fact.evidence_ref
            for fact in target_facts
            if fact.evidence_ref not in depended_on
        }
        if (
            len(source_records) != 1
            or len(series) != 1
            or len(terminal_target_facts) != 1
        ):
            _issue(issues, "observed_not_one_to_one", f"{path}.source")
        if any(
            fact.result_eligibility
            is not EvidenceResultEligibility.OBSERVED_OR_INFERRED
            or fact.payload_type not in {"direct_record", "conversion"}
            or (
                fact.payload_type == "conversion"
                and not fact.registered_reversible_conversion
            )
            for fact in closure
        ):
            _issue(issues, "observed_requires_direct_record", f"{path}.source")

    def _validate_synthesis_refs(
        self,
        contributing_refs: tuple[str, ...],
        evidence_refs: tuple[str, ...],
        *,
        allowed_cells: set[str],
        noncontributing_cells: set[str],
        validated_cells: dict[str, ReasoningCellDraft],
        noncontributing_evidence: dict[str, tuple[str, ...]],
        known_evidence: set[str],
        path: str,
        issues: list[DraftValidationIssue],
    ) -> None:
        if set(contributing_refs) - allowed_cells:
            _issue(
                issues,
                "cell_reference_out_of_scope",
                f"{path}.contributing_cell_refs",
            )
        if allowed_cells and not contributing_refs:
            _issue(
                issues,
                "contributing_cells_missing",
                f"{path}.contributing_cell_refs",
            )
        if not allowed_cells and contributing_refs:
            _issue(
                issues,
                "fixed_cells_cannot_contribute",
                f"{path}.contributing_cell_refs",
            )
        allowed_evidence = {
            ref
            for cell_id in set(contributing_refs) & allowed_cells
            for ref in validated_cells[cell_id].evidence_refs
        } | {
            ref
            for cell_id in noncontributing_cells
            for ref in noncontributing_evidence[cell_id]
        }
        if not set(evidence_refs) <= known_evidence:
            _issue(
                issues,
                "evidence_out_of_scope",
                f"{path}.evidence_refs",
            )
        elif not set(evidence_refs) <= allowed_evidence:
            _issue(
                issues,
                "evidence_reference_not_contributing",
                f"{path}.evidence_refs",
            )
        elif allowed_cells and allowed_evidence and not evidence_refs:
            _issue(
                issues,
                "evidence_references_missing",
                f"{path}.evidence_refs",
            )

    @staticmethod
    def _validate_open_refs(
        evidence_refs: tuple[str, ...],
        cell_refs: tuple[str, ...],
        *,
        known_evidence: set[str],
        known_cells: set[str],
        path: str,
        issues: list[DraftValidationIssue],
    ) -> None:
        if not set(evidence_refs) <= known_evidence:
            _issue(issues, "evidence_out_of_scope", f"{path}.evidence_refs")
        if not set(cell_refs) <= known_cells:
            _issue(issues, "cell_reference_out_of_scope", f"{path}.result_cell_refs")

    def _validate_attribution_basis(
        self,
        basis: AttributionBasis,
        evidence_refs: tuple[str, ...],
        cell_refs: tuple[str, ...],
        *,
        validated_cells: dict[str, ReasoningCellDraft],
        path: str,
        issues: list[DraftValidationIssue],
    ) -> None:
        facts = tuple(self._facts[ref] for ref in evidence_refs if ref in self._facts)
        if basis is AttributionBasis.OBSERVED:
            if any(
                cell_id not in validated_cells
                or validated_cells[cell_id].source is not ResultSource.OBSERVED
                for cell_id in cell_refs
            ):
                _issue(issues, "attribution_basis_invalid", f"{path}.basis")
            observed_refs = {
                ref
                for cell_id in cell_refs
                if cell_id in validated_cells
                and validated_cells[cell_id].source is ResultSource.OBSERVED
                for ref in validated_cells[cell_id].evidence_refs
            }
            if not set(evidence_refs) <= observed_refs:
                _issue(issues, "attribution_basis_invalid", f"{path}.basis")
        elif basis is AttributionBasis.CORRELATED:
            if (
                not facts
                or not any(
                    fact.applicability is EvidenceApplicability.ALLOWED_INDIRECT
                    for fact in facts
                )
                or any(
                    fact.applicability is EvidenceApplicability.CONTEXTUAL_ONLY
                    for fact in facts
                )
                or not _references_are_closed(facts, evidence_refs)
            ):
                _issue(issues, "attribution_basis_invalid", f"{path}.basis")
        elif basis is AttributionBasis.INFERRED:
            if not facts or all(
                fact.applicability is EvidenceApplicability.CONTEXTUAL_ONLY
                for fact in facts
            ) or not _references_are_closed(facts, evidence_refs):
                _issue(issues, "attribution_basis_invalid", f"{path}.basis")
        elif basis is AttributionBasis.HYPOTHESIZED:
            if not facts or any(
                fact.applicability is not EvidenceApplicability.CONTEXTUAL_ONLY
                for fact in facts
            ):
                _issue(issues, "attribution_basis_invalid", f"{path}.basis")


def _evidence_fact(
    item_id: str,
    fragment: EvidenceFragment,
    *,
    capability_registry_digest: str,
    measured_anchors: tuple[MeasuredAnchor, ...] = (),
) -> EvidenceValidationFact:
    payload = fragment.payload
    value = None
    unit_id = None
    aggregation_method = None
    dependencies: tuple[str, ...] = ()
    if isinstance(payload, DirectRecordPayload):
        value = payload.displayed_value
        unit_id = payload.displayed_unit
    elif isinstance(payload, ConversionPayload):
        value = payload.value
        unit_id = payload.unit
        dependencies = (payload.input_evidence_ref.evidence_id,)
    elif isinstance(payload, AggregatePayload):
        value = payload.value
        unit_id = payload.unit
        aggregation_method = AggregationMethod(payload.method.value)
    elif isinstance(payload, (TemporalEstimatePayload, NumericDerivedPayload)):
        value = payload.value
        unit_id = payload.unit
        dependencies = tuple(
            reference.evidence_id for reference in payload.input_evidence_refs
        )
    elif isinstance(payload, RelationSupportPayload):
        dependencies = tuple(
            reference.evidence_id for reference in payload.input_evidence_refs
        )
    source_refs = tuple(
        sorted(
            f"{reference.item_id}#{reference.record_id}"
            for reference in fragment.source_record_refs
        )
    )
    return EvidenceValidationFact(
        evidence_ref=fragment.evidence_id,
        item_id=item_id,
        target_comid=fragment.target_comid,
        time_range=fragment.time_range,
        parameter_refs=fragment.parameter_refs,
        applicability=fragment.applicability,
        result_eligibility=fragment.result_eligibility,
        payload_type=payload.payload_type,
        value=value,
        value_lower=payload.value_lower if isinstance(payload, NumericDerivedPayload) else None,
        value_upper=payload.value_upper if isinstance(payload, NumericDerivedPayload) else None,
        method_kind=(payload.method_kind if isinstance(payload, NumericDerivedPayload)
                     else "explicit_calculation" if isinstance(payload, (AggregatePayload, TemporalEstimatePayload))
                     else None),
        inference_basis=payload.inference_basis if isinstance(payload, NumericDerivedPayload) else None,
        measured_anchors=measured_anchors,
        unit_id=unit_id,
        aggregation_method=aggregation_method,
        source_record_refs=source_refs,
        dependency_evidence_refs=dependencies,
        series_ref=fragment.series_identity,
        quality_class=fragment.quality_class,
        quality_flags=fragment.quality_flags,
        location_comid=fragment.location.comid,
        station_id=fragment.location.station_id,
        temporal_method=(
            payload.method if isinstance(payload, TemporalEstimatePayload) else None
        ),
        anchor_distance_seconds=(
            payload.anchor_distance_seconds
            if isinstance(payload, TemporalEstimatePayload)
            else ()
        ),
        numeric_method_id=(
            payload.method_id if isinstance(payload, NumericDerivedPayload) else None
        ),
        numeric_method_version=(
            payload.method_version
            if isinstance(payload, NumericDerivedPayload)
            else None
        ),
        numeric_task_id=(
            payload.task_id if isinstance(payload, NumericDerivedPayload) else None
        ),
        numeric_plan_digest=(
            payload.plan_digest if isinstance(payload, NumericDerivedPayload) else None
        ),
        numeric_plan_item_id=(
            payload.plan_item_ref.item_id
            if isinstance(payload, NumericDerivedPayload)
            else None
        ),
        numeric_method_registry_digest=(
            payload.method_registry_digest
            if isinstance(payload, NumericDerivedPayload)
            else None
        ),
        numeric_confidence_cap=(
            payload.confidence_cap
            if isinstance(payload, NumericDerivedPayload)
            else None
        ),
        numeric_input_bindings=(
            tuple(
                (reference.evidence_id, reference.item_id, window_id)
                for reference, window_id in zip(
                    payload.input_evidence_refs,
                    payload.input_window_ids,
                    strict=True,
                )
            )
            if isinstance(payload, NumericDerivedPayload)
            else ()
        ),
        registered_reversible_conversion=(
            isinstance(payload, ConversionPayload)
            and fragment.derivation is not None
            and fragment.derivation.algorithm == "decimal_unit_conversion"
            and fragment.derivation.policy_id == payload.conversion_id
            and fragment.derivation.registry_digest == capability_registry_digest
        ),
    )


def _validate_temporal_estimate_fact(
    fact: EvidenceValidationFact,
    facts: dict[str, EvidenceValidationFact],
) -> None:
    dependencies = tuple(facts[ref] for ref in fact.dependency_evidence_refs)
    method = fact.temporal_method
    if (
        fact.applicability is not EvidenceApplicability.ALLOWED_INDIRECT
        or fact.result_eligibility is not EvidenceResultEligibility.INFERRED_ONLY
        or fact.value is None
        or fact.unit_id is None
        or fact.aggregation_method is not None
        or fact.source_record_refs
        or fact.quality_class not in {
            EvidenceQualityClass.VALID,
            EvidenceQualityClass.SUSPICIOUS,
        }
        or fact.series_ref is None
        or fact.location_comid is None
        or fact.station_id is None
        or len(fact.parameter_refs) != 1
        or DataQualityFlag.TEMPORAL_ESTIMATE not in fact.quality_flags
        or method not in {"linear_interpolation", "nearest_hold"}
        or len(dependencies) != (2 if method == "linear_interpolation" else 1)
        or len(fact.anchor_distance_seconds) != len(dependencies)
        or any(distance <= 0 for distance in fact.anchor_distance_seconds)
    ):
        raise ValueError("temporal estimate fact has invalid typed evidence")
    if any(
        anchor.payload_type != "aggregate"
        or anchor.aggregation_method not in {
            AggregationMethod.MEAN,
            AggregationMethod.LATEST,
        }
        or anchor.applicability is not EvidenceApplicability.DIRECT_MEASUREMENT
        or anchor.result_eligibility is not EvidenceResultEligibility.INFERRED_ONLY
        or anchor.quality_class not in {
            EvidenceQualityClass.VALID,
            EvidenceQualityClass.SUSPICIOUS,
        }
        or anchor.target_comid != fact.target_comid
        or anchor.location_comid != fact.location_comid
        or anchor.station_id != fact.station_id
        or anchor.series_ref != fact.series_ref
        or anchor.parameter_refs != fact.parameter_refs
        or anchor.value is None
        or anchor.unit_id != fact.unit_id
        or (anchor.time_range.start < fact.time_range.end
            and anchor.time_range.end > fact.time_range.start)
        for anchor in dependencies
    ):
        raise ValueError("temporal estimate anchors must be matching measured windows")
    if method == "linear_interpolation" and not (
        any(anchor.time_range.end <= fact.time_range.start for anchor in dependencies)
        and any(anchor.time_range.start >= fact.time_range.end for anchor in dependencies)
    ):
        raise ValueError("temporal interpolation requires anchors on both sides")


def _validate_numeric_derived_fact(
    fact: EvidenceValidationFact,
    facts: dict[str, EvidenceValidationFact],
) -> None:
    bindings = fact.numeric_input_bindings
    if (
        fact.applicability is not EvidenceApplicability.ALLOWED_INDIRECT
        or fact.result_eligibility is not EvidenceResultEligibility.INFERRED_ONLY
        or (fact.value is None and fact.method_kind != "model_estimate")
        or fact.unit_id is None
        or fact.aggregation_method is not None
        or fact.source_record_refs
        or fact.quality_class not in {
            EvidenceQualityClass.VALID,
            EvidenceQualityClass.SUSPICIOUS,
        }
        or len(fact.parameter_refs) != 1
        or DataQualityFlag.ESTIMATED not in fact.quality_flags
        or fact.numeric_method_id is None
        or fact.numeric_method_version is None
        or fact.numeric_method_version < 1
        or fact.numeric_task_id is None
        or fact.numeric_plan_digest is None
        or fact.numeric_plan_item_id is None
        or fact.numeric_method_registry_digest is None
        or fact.numeric_confidence_cap is None
        or not 0 <= fact.numeric_confidence_cap <= 1
        or (not bindings and fact.method_kind != "model_estimate")
        or {binding[0] for binding in bindings}
        != set(fact.dependency_evidence_refs)
        or len(bindings) != len(fact.dependency_evidence_refs)
    ):
        raise ValueError("numeric derivation fact has invalid typed evidence")
    if fact.method_kind == "model_estimate":
        from decimal import Decimal

        if (fact.value_lower is None or fact.value_upper is None
                or Decimal(fact.value_lower) >= Decimal(fact.value_upper)
                or (fact.value is not None and (
                    not fact.measured_anchors
                    or not Decimal(fact.value_lower) <= Decimal(fact.value) <= Decimal(fact.value_upper)
                ))
                or bool(fact.measured_anchors) != (fact.inference_basis == "same_parameter_measurement")
                or any(facts[ref].item_id != item_id for ref, item_id, _ in bindings)):
            raise ValueError("model estimate fact has invalid range or provenance")
        return
    for evidence_ref, item_id, _window_id in bindings:
        dependency = facts[evidence_ref]
        if (
            dependency.item_id != item_id
            or dependency.target_comid != fact.target_comid
            or dependency.value is None
            or dependency.unit_id is None
            or dependency.quality_class not in {
                EvidenceQualityClass.VALID,
                EvidenceQualityClass.SUSPICIOUS,
            }
            or dependency.result_eligibility
            is EvidenceResultEligibility.EXPLANATORY_ONLY
            or not set(dependency.quality_flags) <= set(fact.quality_flags)
            or (
                dependency.quality_class is EvidenceQualityClass.SUSPICIOUS
                and fact.quality_class is not EvidenceQualityClass.SUSPICIOUS
            )
            or (
                dependency.payload_type == "numeric_derived"
                and (
                    dependency.numeric_confidence_cap is None
                    or fact.numeric_confidence_cap
                    > dependency.numeric_confidence_cap
                )
            )
            or (
                dependency.payload_type == "temporal_estimate"
                and fact.numeric_confidence_cap
                > _temporal_confidence_cap(dependency, dependency.time_range)
            )
        ):
            raise ValueError("numeric derivation input is incompatible")


def _temporal_confidence_cap(
    fact: EvidenceValidationFact,
    window: TimeRange,
) -> float:
    window_seconds = (window.end - window.start).total_seconds()
    farthest_anchor_seconds = max(fact.anchor_distance_seconds)
    base = 0.75 if fact.temporal_method == "linear_interpolation" else 0.55
    return round(base * min(1.0, window_seconds / farthest_anchor_seconds), 6)


def _fact_closure(
    roots: tuple[EvidenceValidationFact, ...],
    facts: dict[str, EvidenceValidationFact],
) -> tuple[EvidenceValidationFact, ...]:
    result: dict[str, EvidenceValidationFact] = {}
    pending = list(roots)
    while pending:
        fact = pending.pop()
        if fact.evidence_ref in result:
            continue
        result[fact.evidence_ref] = fact
        pending.extend(
            facts[ref]
            for ref in fact.dependency_evidence_refs
            if ref in facts
        )
    return tuple(result[key] for key in sorted(result))


def _references_are_closed(
    facts: tuple[EvidenceValidationFact, ...],
    evidence_refs: tuple[str, ...],
) -> bool:
    return {
        dependency
        for fact in facts
        if fact.payload_type not in {
            "relation_support", "temporal_estimate", "numeric_derived"
        }
        for dependency in fact.dependency_evidence_refs
    } <= set(evidence_refs)


def _quality_flags_from_facts(
    facts: Iterable[EvidenceValidationFact],
) -> tuple[DataQualityFlag, ...]:
    return tuple(
        sorted(
            {flag for fact in facts for flag in fact.quality_flags},
            key=lambda flag: flag.value,
        )
    )


def _fill_single_allowed_units(
    raw: object, batch: ReasoningBatch
) -> tuple[object, tuple[str, ...]]:
    if isinstance(raw, str):
        try:
            value = _strict_json_object(raw)
        except (TypeError, ValueError):
            return raw, ()
    else:
        value = raw
    if not isinstance(value, dict) or not isinstance(value.get("cells"), list):
        return value, ()
    units_by_cell = {
        cell.cell_id: cell.allowed_unit_ids[0]
        for cell in batch.cells
        if len(cell.allowed_unit_ids) == 1
    }
    cells = list(value["cells"])
    paths = []
    for index, cell in enumerate(cells):
        if not isinstance(cell, dict) or not isinstance(cell.get("cell_id"), str):
            continue
        unit = units_by_cell.get(cell["cell_id"])
        if (
            unit is not None
            and cell.get("outcome_status") in {
                OutcomeStatus.QUANTIFIED.value, OutcomeStatus.RANGE_ESTIMATE.value
            }
            and "target_unit_id" in cell
            and cell["target_unit_id"] is None
        ):
            cells[index] = {**cell, "target_unit_id": unit}
            paths.append(f"$.cells[{index}].target_unit_id")
    return {**value, "cells": cells}, tuple(paths)


def _fill_cited_numeric_ranges(
    raw: object,
    batch: ReasoningBatch,
    facts: dict[str, EvidenceValidationFact],
    scopes: dict[str, CellEvidenceValidationScope],
) -> tuple[object, tuple[str, ...]]:
    if not isinstance(raw, dict) or not isinstance(raw.get("cells"), list):
        return raw, ()
    planned_by_id = {cell.cell_id: cell for cell in batch.cells}
    cells = list(raw["cells"])
    filled_paths: list[str] = []
    for index, cell in enumerate(cells):
        if not isinstance(cell, dict):
            continue
        status = cell.get("outcome_status")
        if status == OutcomeStatus.RANGE_ESTIMATE.value:
            if (
                "value_lower" in cell
                or "value_upper" in cell
                or "representative_value" not in cell
                or cell["representative_value"] is not None
            ):
                continue
        elif status == OutcomeStatus.QUANTIFIED.value:
            bounds_missing = (
                "value_lower" not in cell and "value_upper" not in cell
            )
            bounds_null = (
                "value_lower" in cell
                and "value_upper" in cell
                and cell["value_lower"] is None
                and cell["value_upper"] is None
            )
            if not (bounds_missing or bounds_null) or not isinstance(
                cell.get("representative_value"), str
            ):
                continue
        else:
            continue
        cell_id = cell.get("cell_id")
        planned = planned_by_id.get(cell_id) if isinstance(cell_id, str) else None
        scope = scopes.get(cell_id) if isinstance(cell_id, str) else None
        refs = cell.get("evidence_refs")
        if (
            planned is None
            or scope is None
            or not isinstance(refs, (list, tuple))
            or not refs
            or any(not isinstance(ref, str) for ref in refs)
            or len(set(refs)) != len(refs)
            or not set(refs) <= set(planned.allowed_evidence_refs)
        ):
            continue
        candidates = tuple(
            facts[ref]
            for ref in refs
            if ref in scope.target_indirect_refs
            and facts[ref].payload_type == "numeric_derived"
            and facts[ref].method_kind == "model_estimate"
            and (facts[ref].value is None) == (
                status == OutcomeStatus.RANGE_ESTIMATE.value
            )
            and facts[ref].value_lower is not None
            and facts[ref].value_upper is not None
        )
        if len(candidates) != 1:
            continue
        fact = candidates[0]
        if (
            fact.parameter_refs != (planned.parameter,)
            or fact.time_range.start != planned.start_time
            or fact.time_range.end != planned.end_time
            or fact.unit_id != cell.get("target_unit_id")
            or fact.value != cell["representative_value"]
        ):
            continue
        cells[index] = {
            **cell,
            "value_lower": fact.value_lower,
            "value_upper": fact.value_upper,
        }
        filled_paths.extend((
            f"$.cells[{index}].value_lower",
            f"$.cells[{index}].value_upper",
        ))
    return {**raw, "cells": cells}, tuple(filled_paths)


def _parse_draft(raw: object, model_type, *, guideline_aliases=frozenset()):
    try:
        value = _strict_json_object(raw) if isinstance(raw, str) else raw
        value = sanitize_guideline_refs(value, guideline_aliases)
        return model_type.model_validate(value)
    except ValidationError as error:
        issues = []
        for item in error.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        ):
            error_type = item["type"]
            code = (
                "missing_field"
                if error_type == "missing"
                else "extra_field"
                if error_type == "extra_forbidden"
                else "invalid_enum"
                if "enum" in error_type or "literal" in error_type
                else "invalid_type"
                if "type" in error_type or "parsing" in error_type
                else "invalid_value"
            )
            path = "$" + "".join(
                f"[{part}]" if isinstance(part, int) else f".{part}"
                for part in item["loc"]
            )
            issues.append(DraftValidationIssue(code=code, path=path))
        raise DraftValidationError(_canonical_issues(issues)) from None
    except (TypeError, ValueError, json.JSONDecodeError):
        raise DraftValidationError(
            (DraftValidationIssue(code="invalid_json", path="$"),)
        ) from None


def _strict_json_object(value: str) -> dict[str, object]:
    def reject_duplicates(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result

    def reject_constant(_value):
        raise ValueError("non-finite JSON number")

    parsed = json.loads(
        value,
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
    )
    if not isinstance(parsed, dict):
        raise TypeError("draft must be one JSON object")
    return parsed


def _issue(
    issues: list[DraftValidationIssue],
    code: str,
    path: str,
) -> None:
    issues.append(DraftValidationIssue(code=code, path=path))


def _raise_issues(issues: Iterable[DraftValidationIssue]) -> None:
    canonical = _canonical_issues(issues)
    if canonical:
        raise DraftValidationError(canonical)


def _canonical_issues(
    issues: Iterable[DraftValidationIssue],
) -> tuple[DraftValidationIssue, ...]:
    return tuple(
        DraftValidationIssue(code=code, path=path)
        for code, path in sorted({(issue.code, issue.path) for issue in issues})
    )
