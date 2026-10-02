from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Self
from uuid import UUID

from pydantic import StrictBool, ValidationError, field_validator, model_validator

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
    DomainModel,
    EvidenceApplicability,
    EvidenceCellRole,
    EvidenceFragment,
    EvidenceQualityClass,
    EvidenceResultEligibility,
    IndeterminateReason,
    InferenceMode,
    NonEmptyString,
    OutcomeStatus,
    ParameterKey,
    RelationSupportPayload,
    ResultSource,
    RiskLevel,
    TimeRange,
    ensure_unique_tuple,
)


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
    unit_id: NonEmptyString | None
    aggregation_method: AggregationMethod | None
    source_record_refs: tuple[NonEmptyString, ...]
    dependency_evidence_refs: tuple[NonEmptyString, ...]
    series_ref: NonEmptyString | None
    quality_class: EvidenceQualityClass | None
    quality_flags: tuple[DataQualityFlag, ...]
    registered_reversible_conversion: StrictBool

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

    facts = tuple(
        sorted(
            (
                _evidence_fact(
                    reference_by_id[fragment.evidence_id].item_id,
                    fragment,
                    capability_registry_digest=sealed.capability_registry_digest,
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
    scopes = []
    windows = {window.window_id: window for window in sealed.windows}
    for gate in sealed.gates:
        target = cells[
            (gate.parameter, gate.window_id, EvidenceCellRole.TARGET)
        ]
        support = cells[
            (discharge, gate.window_id, EvidenceCellRole.REQUIRED_SUPPORT)
        ]
        scope = CellEvidenceValidationScope(
            cell_id=gate.cell_id,
            parameter=gate.parameter,
            time_range=windows[gate.window_id].time_range,
            target_direct_refs=tuple(sorted(target.direct_fragment_refs)),
            target_indirect_refs=tuple(
                sorted(target.allowed_indirect_fragment_refs)
            ),
            support_direct_refs=tuple(sorted(support.direct_fragment_refs)),
            support_indirect_refs=tuple(
                sorted(support.allowed_indirect_fragment_refs)
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
    ) -> ValidatedReasoningBatch:
        self._validate_batch_identity(batch)
        draft = _parse_draft(raw, ReasoningBatchDraft)
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
        for index, cell in enumerate(draft.cells):
            planned = scopes.get(cell.cell_id)
            if planned is not None:
                self._validate_cell(
                    cell,
                    planned=planned,
                    path=f"$.cells[{index}]",
                    issues=issues,
                )
        _raise_issues(issues)
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
    ) -> ValidatedOverallSynthesis:
        draft = _parse_draft(raw, OverallSynthesisDraft)
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
            allowed_cells = parameter_cells & set(validated_cells)
            self._validate_synthesis_refs(
                series.contributing_cell_refs,
                series.evidence_refs,
                allowed_cells=allowed_cells,
                fixed_cells=parameter_cells & set(fixed_by_id),
                validated_cells=validated_cells,
                fixed_by_id=fixed_by_id,
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
            allowed_cells = window_cells & set(validated_cells)
            self._validate_synthesis_refs(
                window.contributing_cell_refs,
                window.evidence_refs,
                allowed_cells=allowed_cells,
                fixed_cells=window_cells & set(fixed_by_id),
                validated_cells=validated_cells,
                fixed_by_id=fixed_by_id,
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
            allowed_cells=set(validated_cells),
            fixed_cells=set(fixed_by_id),
            validated_cells=validated_cells,
            fixed_by_id=fixed_by_id,
            known_evidence=known_evidence,
            path="$.overall",
            issues=issues,
        )
        if draft.overall.risk_level is RiskLevel.NONE and (
            not draft.overall.contributing_cell_refs
            or not draft.overall.evidence_refs
        ):
            _issue(issues, "none_risk_without_evidence", "$.overall.risk_level")

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
        if not refs <= set(planned.allowed_evidence_refs):
            _issue(issues, "evidence_out_of_scope", f"{path}.evidence_refs")
            return
        facts = tuple(self._facts[ref] for ref in cell.evidence_refs)
        dependencies = {
            dependency for fact in facts for dependency in fact.dependency_evidence_refs
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
        expected_flags = tuple(
            sorted(
                {flag for fact in facts for flag in fact.quality_flags},
                key=lambda flag: flag.value,
            )
        )
        if cell.quality_flags != expected_flags:
            _issue(issues, "quality_flags_mismatch", f"{path}.quality_flags")
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
        if cell.outcome_status is OutcomeStatus.QUANTIFIED:
            if not target_direct:
                _issue(issues, "only_indirect_quantified", f"{path}.outcome_status")
            candidates = tuple(
                fact
                for fact in facts
                if fact.evidence_ref in target_direct
                and fact.value == cell.representative_value
                and fact.unit_id == cell.target_unit_id
            )
            if not candidates:
                _issue(
                    issues,
                    "quantified_value_not_backed",
                    f"{path}.representative_value",
                )
            elif not any(
                fact.aggregation_method is cell.aggregation_method
                for fact in candidates
            ):
                _issue(
                    issues,
                    "aggregation_mismatch",
                    f"{path}.aggregation_method",
                )
        elif (
            cell.outcome_status is OutcomeStatus.DIRECTIONAL
            and not target_direct
            and not target_indirect
        ):
            _issue(issues, "directional_target_evidence_missing", f"{path}.evidence_refs")
        if cell.source is ResultSource.OBSERVED:
            self._validate_observed(
                facts,
                target_direct=target_direct,
                target_indirect=target_indirect,
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
        target_indirect: set[str],
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
            target_indirect
            or len(source_records) != 1
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
        fixed_cells: set[str],
        validated_cells: dict[str, ReasoningCellDraft],
        fixed_by_id: dict[str, object],
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
            for cell_id in fixed_cells
            for ref in fixed_by_id[cell_id].result.evidence_refs
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
        elif allowed_evidence and not evidence_refs:
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
        unit_id=unit_id,
        aggregation_method=aggregation_method,
        source_record_refs=source_refs,
        dependency_evidence_refs=dependencies,
        series_ref=fragment.series_identity,
        quality_class=fragment.quality_class,
        quality_flags=fragment.quality_flags,
        registered_reversible_conversion=(
            isinstance(payload, ConversionPayload)
            and fragment.derivation is not None
            and fragment.derivation.algorithm == "decimal_unit_conversion"
            and fragment.derivation.policy_id == payload.conversion_id
            and fragment.derivation.registry_digest == capability_registry_digest
        ),
    )


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
        for dependency in fact.dependency_evidence_refs
    } <= set(evidence_refs)


def _parse_draft(raw: object, model_type):
    try:
        value = _strict_json_object(raw) if isinstance(raw, str) else raw
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
        raise ValueError("draft must be one JSON object")
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
