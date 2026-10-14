from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, Protocol, Self
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, StrictBool, StrictInt, ValidationError, model_validator

from app.application.reasoning.errors import (
    ReasoningPlanError,
    ReasoningPlanTooLargeError,
)
from app.application.reasoning.model_models import WaterbodyJsonSchema
from app.application.reasoning.prompt_builder import (
    FixedCellPromptSummary,
    OverallSynthesisPromptInput,
    PromptBuilder,
    ReasoningBatchPromptInput,
    ValidatedCellPromptSummary,
)
from app.application.reasoning.prompt_policy import (
    canonical_json_digest,
    canonical_json_text,
)
from app.application.reasoning.result_models import (
    AgentReasoningCellScope,
    ResultSkeleton,
    validate_result_skeleton,
)
from app.application.reasoning.working_context import (
    BoundedEvidenceSummary,
    WorkingContextFact,
    WorkingContextFactKind,
    WorkingContextProjection,
    build_working_context_projection,
)
from app.domain import (
    DomainModel,
    ItemReference,
    ModelCatalogSnapshot,
    ModelRole,
    ModelSelection,
    NonEmptyString,
    OutcomeStatus,
    ParameterKey,
    RiskLevel,
    TimeSegment,
    UtcDatetime,
    ensure_unique_tuple,
)


_HARD_MAX_CELLS_PER_BATCH = 32
_HARD_MAX_BATCHES = 24
_MAX_SYNTHESIS_BASE_FACTS = 125


class _BatchContextTooLarge(ValueError):
    pass


class ModelFitCapability(DomainModel):
    """Provider-adapter limits for one catalog-authorized model."""

    model: ModelSelection
    context_window_tokens: Annotated[int, Field(strict=True, ge=1)]
    max_output_tokens: Annotated[int, Field(strict=True, ge=1)]

    @model_validator(mode="after")
    def validate_token_limits(self) -> Self:
        if self.max_output_tokens > self.context_window_tokens:
            raise ValueError("model output limit cannot exceed context window")
        return self


class ModelSizeEstimate(DomainModel):
    input_tokens: Annotated[int, Field(strict=True, ge=0)]
    output_tokens: Annotated[int, Field(strict=True, ge=0)]
    estimated_output_bytes: Annotated[int, Field(strict=True, ge=0)]


class ModelSizeEstimator(Protocol):
    """Adapter-owned estimator over exact canonical request/schema bytes."""

    def estimate(
        self,
        model: ModelSelection,
        *,
        serialized_request: bytes,
        serialized_response_shape: bytes,
    ) -> ModelSizeEstimate: ...


@dataclass(frozen=True, slots=True)
class ReasoningPlannerLimits:
    max_cells_per_batch: int = _HARD_MAX_CELLS_PER_BATCH
    max_batches: int = _HARD_MAX_BATCHES
    max_serialized_input_bytes: int = 262_144
    max_serialized_output_bytes: int = 262_144

    def __post_init__(self) -> None:
        values = (
            self.max_cells_per_batch,
            self.max_batches,
            self.max_serialized_input_bytes,
            self.max_serialized_output_bytes,
        )
        if any(type(value) is not int or value <= 0 for value in values):
            raise ValueError("reasoning planner limits must be positive integers")
        if self.max_cells_per_batch > _HARD_MAX_CELLS_PER_BATCH:
            raise ValueError("reasoning batch hard limit is 32 cells")
        if self.max_batches > _HARD_MAX_BATCHES:
            raise ValueError("reasoning plan hard limit is 24 batches")


class ReasoningBatch(DomainModel):
    batch_id: NonEmptyString
    batch_index: Annotated[int, Field(strict=True, ge=0)]
    run_id: UUID
    session_id: UUID
    result_id: NonEmptyString
    model: ModelSelection
    scope_digest: NonEmptyString
    skeleton_digest: NonEmptyString
    bundle_ref: ItemReference
    bundle_digest: NonEmptyString
    rule_plan_digest: NonEmptyString
    capability_registry_digest: NonEmptyString
    parameter: ParameterKey
    segment_index: Annotated[int, Field(strict=True, ge=0)]
    segment: TimeSegment
    operation_id: UUID
    deadline: UtcDatetime
    context: WorkingContextProjection
    response_schema: WaterbodyJsonSchema
    cells: tuple[AgentReasoningCellScope, ...]
    serialized_input_bytes: Annotated[int, Field(strict=True, ge=1)]
    response_shape_bytes: Annotated[int, Field(strict=True, ge=1)]
    estimated_input_tokens: Annotated[int, Field(strict=True, ge=0)]
    estimated_output_tokens: Annotated[int, Field(strict=True, ge=0)]
    estimated_output_bytes: Annotated[int, Field(strict=True, ge=0)]
    batch_digest: NonEmptyString

    @model_validator(mode="after")
    def validate_batch(self) -> Self:
        if not self.cells or len(self.cells) > _HARD_MAX_CELLS_PER_BATCH:
            raise ValueError("reasoning batch must contain between 1 and 32 cells")
        ensure_unique_tuple(
            tuple(cell.cell_id for cell in self.cells),
            field_name="reasoning batch cells",
        )
        if any(cell.parameter != self.parameter for cell in self.cells):
            raise ValueError("reasoning batch cannot cross parameters")
        if any(
            cell.mode is not self.segment.mode
            or cell.start_time < self.segment.start
            or cell.end_time > self.segment.end
            for cell in self.cells
        ):
            raise ValueError("reasoning batch cannot cross inference segments")
        if tuple(cell.start_time for cell in self.cells) != tuple(
            sorted(cell.start_time for cell in self.cells)
        ):
            raise ValueError("reasoning batch cells must be chronological")
        if (
            self.context.run_id != self.run_id
            or self.context.session_id != self.session_id
        ):
            raise ValueError("reasoning batch context identity mismatch")
        if self.context.scope_digest != self.scope_digest:
            raise ValueError("reasoning batch scope digest mismatch")
        if (
            self.context.bundle_ref != self.bundle_ref
            or self.context.bundle_digest != self.bundle_digest
            or self.context.rule_plan_digest != self.rule_plan_digest
            or self.context.capability_registry_digest
            != self.capability_registry_digest
        ):
            raise ValueError("reasoning batch evidence identity mismatch")
        if self.batch_id != _batch_id(
            batch_index=self.batch_index,
            run_id=self.run_id,
            session_id=self.session_id,
            result_id=self.result_id,
            model=self.model,
            scope_digest=self.scope_digest,
            bundle_digest=self.bundle_digest,
            rule_plan_digest=self.rule_plan_digest,
            capability_registry_digest=self.capability_registry_digest,
            parameter=self.parameter,
            segment_index=self.segment_index,
            cell_ids=tuple(cell.cell_id for cell in self.cells),
        ):
            raise ValueError("reasoning batch identity is not canonical")
        if self.operation_id != uuid5(NAMESPACE_URL, self.batch_id):
            raise ValueError("reasoning batch operation identity is not canonical")
        expected_digest = canonical_json_digest(
            self.model_dump(mode="json", exclude={"batch_digest"})
        )
        if self.batch_digest != expected_digest:
            raise ValueError("reasoning batch digest does not match canonical content")
        return self

    def prompt_input(self) -> ReasoningBatchPromptInput:
        return ReasoningBatchPromptInput(
            operation_id=self.operation_id,
            model=self.model,
            deadline=self.deadline,
            context=self.context,
            response_schema=self.response_schema,
            batch_id=self.batch_id,
            cells=self.cells,
        )


class ReasoningBatchPlan(DomainModel):
    run_id: UUID
    session_id: UUID
    result_id: NonEmptyString
    model: ModelSelection
    scope_digest: NonEmptyString
    skeleton_digest: NonEmptyString
    bundle_ref: ItemReference
    bundle_digest: NonEmptyString
    rule_plan_digest: NonEmptyString
    capability_registry_digest: NonEmptyString
    supported_cell_order: tuple[NonEmptyString, ...]
    batches: tuple[ReasoningBatch, ...]
    batch_count: Annotated[int, Field(strict=True, ge=0, le=_HARD_MAX_BATCHES)]
    supported_cell_count: Annotated[int, Field(strict=True, ge=0)]
    total_serialized_input_bytes: Annotated[int, Field(strict=True, ge=0)]
    plan_digest: NonEmptyString

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        ensure_unique_tuple(
            self.supported_cell_order,
            field_name="reasoning plan supported cells",
        )
        if self.batch_count != len(self.batches):
            raise ValueError("reasoning batch count is not authoritative")
        if self.supported_cell_count != len(self.supported_cell_order):
            raise ValueError("reasoning supported-cell count is not authoritative")
        if self.total_serialized_input_bytes != sum(
            batch.serialized_input_bytes for batch in self.batches
        ):
            raise ValueError("reasoning serialized-input count is not authoritative")
        flattened = tuple(
            cell.cell_id for batch in self.batches for cell in batch.cells
        )
        if flattened != self.supported_cell_order:
            raise ValueError(
                "reasoning batches must exactly preserve supported-cell order"
            )
        for index, batch in enumerate(self.batches):
            if batch.batch_index != index:
                raise ValueError("reasoning batch indexes must be consecutive")
            if (
                batch.run_id != self.run_id
                or batch.session_id != self.session_id
                or batch.result_id != self.result_id
                or batch.model != self.model
                or batch.scope_digest != self.scope_digest
                or batch.skeleton_digest != self.skeleton_digest
                or batch.bundle_ref != self.bundle_ref
                or batch.bundle_digest != self.bundle_digest
                or batch.rule_plan_digest != self.rule_plan_digest
                or batch.capability_registry_digest
                != self.capability_registry_digest
            ):
                raise ValueError("reasoning batch identity drifts from its plan")
        expected_digest = canonical_json_digest(
            self.model_dump(mode="json", exclude={"plan_digest"})
        )
        if self.plan_digest != expected_digest:
            raise ValueError(
                "reasoning batch plan digest does not match canonical content"
            )
        return self


class SynthesisCoveragePlan(DomainModel):
    planned_cells: Annotated[int, Field(strict=True, ge=0)]
    supported_cells: Annotated[int, Field(strict=True, ge=0)]
    fixed_cells: Annotated[int, Field(strict=True, ge=0)]
    blocked_cells: Annotated[int, Field(strict=True, ge=0)]
    unsupported_cells: Annotated[int, Field(strict=True, ge=0)]
    model_editable: Literal[False] = False

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.supported_cells + self.fixed_cells != self.planned_cells:
            raise ValueError("synthesis coverage must account for every planned cell")
        if self.blocked_cells + self.unsupported_cells != self.fixed_cells:
            raise ValueError("synthesis fixed-cell counts are inconsistent")
        return self


class SynthesisInputPlan(DomainModel):
    run_id: UUID
    session_id: UUID
    result_id: NonEmptyString
    model: ModelSelection
    scope_digest: NonEmptyString
    skeleton_digest: NonEmptyString
    bundle_ref: ItemReference
    bundle_digest: NonEmptyString
    rule_plan_digest: NonEmptyString
    capability_registry_digest: NonEmptyString
    operation_id: UUID
    deadline: UtcDatetime
    response_schema: WaterbodyJsonSchema
    context: WorkingContextProjection
    validated_cell_order: tuple[NonEmptyString, ...]
    validated_cell_scopes: tuple[AgentReasoningCellScope, ...]
    fixed_cells: tuple[FixedCellPromptSummary, ...]
    coverage: SynthesisCoveragePlan
    requires_model_call: StrictBool
    serialized_base_input_bytes: Annotated[int, Field(strict=True, ge=0)]
    reserved_validated_result_bytes: Annotated[int, Field(strict=True, ge=0)]
    estimated_input_tokens: Annotated[int, Field(strict=True, ge=0)]
    estimated_output_tokens: Annotated[int, Field(strict=True, ge=0)]
    estimated_output_bytes: Annotated[int, Field(strict=True, ge=0)]
    synthesis_digest: NonEmptyString

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        ensure_unique_tuple(
            self.validated_cell_order,
            field_name="synthesis validated-cell order",
        )
        if tuple(cell.cell_id for cell in self.validated_cell_scopes) != (
            self.validated_cell_order
        ):
            raise ValueError("synthesis validated-cell scopes are incomplete")
        fixed_ids = tuple(cell.cell_id for cell in self.fixed_cells)
        ensure_unique_tuple(fixed_ids, field_name="synthesis fixed cells")
        if set(fixed_ids) & set(self.validated_cell_order):
            raise ValueError("synthesis fixed and model cells must be disjoint")
        if self.requires_model_call is not bool(self.validated_cell_order):
            raise ValueError(
                "synthesis call requirement must follow supported coverage"
            )
        if len(self.validated_cell_order) != self.coverage.supported_cells:
            raise ValueError("synthesis supported-cell order is incomplete")
        if len(self.fixed_cells) != self.coverage.fixed_cells:
            raise ValueError("synthesis fixed-cell plan is incomplete")
        if (
            self.context.run_id != self.run_id
            or self.context.session_id != self.session_id
        ):
            raise ValueError("synthesis context identity mismatch")
        if self.context.scope_digest != self.scope_digest:
            raise ValueError("synthesis scope digest mismatch")
        if (
            self.context.bundle_ref != self.bundle_ref
            or self.context.bundle_digest != self.bundle_digest
            or self.context.rule_plan_digest != self.rule_plan_digest
            or self.context.capability_registry_digest
            != self.capability_registry_digest
        ):
            raise ValueError("synthesis evidence identity mismatch")
        if self.operation_id != uuid5(
            NAMESPACE_URL,
            f"synthesis:{self.run_id}:{self.session_id}:"
            f"{self.result_id}:{self.scope_digest}",
        ):
            raise ValueError("synthesis operation identity is not canonical")
        metrics = (
            self.serialized_base_input_bytes,
            self.reserved_validated_result_bytes,
            self.estimated_input_tokens,
            self.estimated_output_tokens,
            self.estimated_output_bytes,
        )
        if not self.requires_model_call and any(metrics):
            raise ValueError("all-fixed synthesis must not reserve a model call")
        expected_digest = canonical_json_digest(
            self.model_dump(mode="json", exclude={"synthesis_digest"})
        )
        if self.synthesis_digest != expected_digest:
            raise ValueError("synthesis plan digest does not match canonical content")
        return self

    def prompt_input(
        self,
        validated_cells: tuple[ValidatedCellPromptSummary, ...],
    ) -> OverallSynthesisPromptInput:
        if not self.requires_model_call:
            raise ReasoningPlanError("reasoning_synthesis_not_required")
        if tuple(cell.cell_id for cell in validated_cells) != self.validated_cell_order:
            raise ReasoningPlanError("reasoning_synthesis_cells_incomplete")
        for summary, scope in zip(validated_cells, self.validated_cell_scopes):
            if summary.window_id != scope.window_id:
                raise ReasoningPlanError("reasoning_synthesis_cell_identity_mismatch")
            if not set(summary.evidence_refs) <= set(scope.allowed_evidence_refs):
                raise ReasoningPlanError("reasoning_synthesis_evidence_out_of_scope")
        return OverallSynthesisPromptInput(
            operation_id=self.operation_id,
            model=self.model,
            deadline=self.deadline,
            context=self.context,
            response_schema=self.response_schema,
            validated_cells=validated_cells,
            fixed_cells=self.fixed_cells,
        )


class ReasoningPlan(DomainModel):
    batch_plan: ReasoningBatchPlan
    synthesis_plan: SynthesisInputPlan
    plan_digest: NonEmptyString

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        batch = self.batch_plan
        synthesis = self.synthesis_plan
        if (
            batch.run_id != synthesis.run_id
            or batch.session_id != synthesis.session_id
            or batch.result_id != synthesis.result_id
            or batch.model != synthesis.model
            or batch.scope_digest != synthesis.scope_digest
            or batch.skeleton_digest != synthesis.skeleton_digest
            or batch.bundle_ref != synthesis.bundle_ref
            or batch.bundle_digest != synthesis.bundle_digest
            or batch.rule_plan_digest != synthesis.rule_plan_digest
            or batch.capability_registry_digest
            != synthesis.capability_registry_digest
            or batch.supported_cell_order != synthesis.validated_cell_order
            or tuple(
                cell for planned_batch in batch.batches for cell in planned_batch.cells
            )
            != synthesis.validated_cell_scopes
        ):
            raise ValueError("batch and synthesis plans must share one identity")
        expected_digest = canonical_json_digest(
            self.model_dump(mode="json", exclude={"plan_digest"})
        )
        if self.plan_digest != expected_digest:
            raise ValueError("reasoning plan digest does not match canonical content")
        return self


class ReasoningBatchPlanner:
    def __init__(
        self,
        *,
        prompt_builder: PromptBuilder,
        model_catalog: ModelCatalogSnapshot,
        size_estimator: ModelSizeEstimator,
        limits: ReasoningPlannerLimits = ReasoningPlannerLimits(),
    ) -> None:
        self._prompt_builder = prompt_builder
        self._model_catalog = model_catalog
        self._size_estimator = size_estimator
        self._limits = limits

    def plan(
        self,
        skeleton: ResultSkeleton,
        *,
        working_context: WorkingContextProjection,
        evidence_summaries: tuple[BoundedEvidenceSummary, ...] | None = None,
        model_fit: ModelFitCapability,
        deadline: datetime,
        batch_response_schema: WaterbodyJsonSchema,
        synthesis_response_schema: WaterbodyJsonSchema,
    ) -> ReasoningPlan:
        try:
            skeleton = validate_result_skeleton(skeleton)
        except ValueError as error:
            raise ReasoningPlanError("reasoning_skeleton_invalid") from error
        model = model_fit.model
        if not self._model_catalog.supports(model, ModelRole.WATERBODY):
            raise ReasoningPlanError("reasoning_model_not_authorized")
        self._validate_context(skeleton, working_context)

        projection = skeleton.agent_projection()
        groups = _stable_consecutive_groups(skeleton, projection.cells)
        batches: list[ReasoningBatch] = []
        for parameter, segment_index, cells in groups:
            current: tuple[AgentReasoningCellScope, ...] = ()
            current_batch: ReasoningBatch | None = None
            for cell in cells:
                candidate_cells = (*current, cell)
                if len(candidate_cells) > self._limits.max_cells_per_batch:
                    self._append_batch(batches, current_batch)
                    candidate_cells = (cell,)
                candidate, fits = self._measure_batch(
                    skeleton=skeleton,
                    base_context=working_context,
                    evidence_summaries=evidence_summaries,
                    model_fit=model_fit,
                    deadline=deadline,
                    response_schema=batch_response_schema,
                    batch_index=len(batches),
                    parameter=parameter,
                    segment_index=segment_index,
                    cells=candidate_cells,
                )
                if not fits and len(candidate_cells) > 1:
                    self._append_batch(batches, current_batch)
                    candidate_cells = (cell,)
                    candidate, fits = self._measure_batch(
                        skeleton=skeleton,
                        base_context=working_context,
                        evidence_summaries=evidence_summaries,
                        model_fit=model_fit,
                        deadline=deadline,
                        response_schema=batch_response_schema,
                        batch_index=len(batches),
                        parameter=parameter,
                        segment_index=segment_index,
                        cells=candidate_cells,
                    )
                if not fits:
                    raise ReasoningPlanTooLargeError()
                current = candidate_cells
                current_batch = candidate
            self._append_batch(batches, current_batch)

        expected_order = tuple(cell.cell_id for cell in projection.cells)
        flattened = tuple(cell.cell_id for batch in batches for cell in batch.cells)
        if flattened != expected_order:
            raise ReasoningPlanError("reasoning_plan_incomplete")
        batch_values = {
            "run_id": skeleton.run_id,
            "session_id": skeleton.session_id,
            "result_id": skeleton.result_id,
            "model": model,
            "scope_digest": projection.scope_digest,
            "skeleton_digest": skeleton.skeleton_digest,
            "bundle_ref": skeleton.bundle_ref,
            "bundle_digest": skeleton.bundle_digest,
            "rule_plan_digest": skeleton.rule_plan_digest,
            "capability_registry_digest": skeleton.capability_registry_digest,
            "supported_cell_order": expected_order,
            "batches": tuple(batches),
            "batch_count": len(batches),
            "supported_cell_count": len(expected_order),
            "total_serialized_input_bytes": sum(
                batch.serialized_input_bytes for batch in batches
            ),
        }
        batch_plan = ReasoningBatchPlan(
            **batch_values,
            plan_digest=canonical_json_digest(batch_values),
        )
        synthesis_plan = self._build_synthesis_plan(
            skeleton=skeleton,
            projection_scope_digest=projection.scope_digest,
            base_context=working_context,
            model_fit=model_fit,
            deadline=deadline,
            response_schema=synthesis_response_schema,
            batch_plan=batch_plan,
        )
        values = {
            "batch_plan": batch_plan,
            "synthesis_plan": synthesis_plan,
        }
        return ReasoningPlan(
            **values,
            plan_digest=canonical_json_digest(values),
        )

    def _append_batch(
        self,
        batches: list[ReasoningBatch],
        batch: ReasoningBatch | None,
    ) -> None:
        if batch is None:
            return
        batches.append(batch)
        if len(batches) > self._limits.max_batches:
            raise ReasoningPlanTooLargeError()

    def _measure_batch(
        self,
        *,
        skeleton: ResultSkeleton,
        base_context: WorkingContextProjection,
        evidence_summaries: tuple[BoundedEvidenceSummary, ...] | None,
        model_fit: ModelFitCapability,
        deadline: datetime,
        response_schema: WaterbodyJsonSchema,
        batch_index: int,
        parameter: ParameterKey,
        segment_index: int,
        cells: tuple[AgentReasoningCellScope, ...],
    ) -> tuple[ReasoningBatch | None, bool]:
        try:
            context = _batch_context(
                skeleton,
                base_context,
                evidence_summaries=evidence_summaries,
                scope_digest=skeleton.agent_projection().scope_digest,
                cells=cells,
            )
        except _BatchContextTooLarge:
            return None, False
        model = model_fit.model
        batch_id = _batch_id(
            batch_index=batch_index,
            run_id=skeleton.run_id,
            session_id=skeleton.session_id,
            result_id=skeleton.result_id,
            model=model,
            scope_digest=context.scope_digest,
            bundle_digest=skeleton.bundle_digest,
            rule_plan_digest=skeleton.rule_plan_digest,
            capability_registry_digest=skeleton.capability_registry_digest,
            parameter=parameter,
            segment_index=segment_index,
            cell_ids=tuple(cell.cell_id for cell in cells),
        )
        operation_id = uuid5(NAMESPACE_URL, batch_id)
        prompt = ReasoningBatchPromptInput(
            operation_id=operation_id,
            model=model,
            deadline=deadline,
            context=context,
            response_schema=response_schema,
            batch_id=batch_id,
            cells=cells,
        )
        request = self._prompt_builder.build_reasoning_batch(prompt)
        request_bytes = _canonical_bytes(request.model_dump(mode="json"))
        response_shape = _response_shape_bytes(
            response_schema,
            tuple(cell.cell_id for cell in cells),
        )
        estimate = self._size_estimator.estimate(
            model,
            serialized_request=request_bytes,
            serialized_response_shape=response_shape,
        )
        values = {
            "batch_id": batch_id,
            "batch_index": batch_index,
            "run_id": skeleton.run_id,
            "session_id": skeleton.session_id,
            "result_id": skeleton.result_id,
            "model": model,
            "scope_digest": context.scope_digest,
            "skeleton_digest": skeleton.skeleton_digest,
            "bundle_ref": skeleton.bundle_ref,
            "bundle_digest": skeleton.bundle_digest,
            "rule_plan_digest": skeleton.rule_plan_digest,
            "capability_registry_digest": skeleton.capability_registry_digest,
            "parameter": parameter,
            "segment_index": segment_index,
            "segment": skeleton.time_segments[segment_index],
            "operation_id": operation_id,
            "deadline": deadline,
            "context": context,
            "response_schema": response_schema,
            "cells": cells,
            "serialized_input_bytes": len(request_bytes),
            "response_shape_bytes": len(response_shape),
            "estimated_input_tokens": estimate.input_tokens,
            "estimated_output_tokens": estimate.output_tokens,
            "estimated_output_bytes": estimate.estimated_output_bytes,
        }
        batch = ReasoningBatch(
            **values,
            batch_digest=canonical_json_digest(values),
        )
        return batch, self._fits(
            serialized_input_bytes=len(request_bytes),
            estimate=estimate,
            model_fit=model_fit,
        )

    def _build_synthesis_plan(
        self,
        *,
        skeleton: ResultSkeleton,
        projection_scope_digest: str,
        base_context: WorkingContextProjection,
        model_fit: ModelFitCapability,
        deadline: datetime,
        response_schema: WaterbodyJsonSchema,
        batch_plan: ReasoningBatchPlan,
    ) -> SynthesisInputPlan:
        context = _synthesis_context(
            skeleton,
            base_context,
            scope_digest=projection_scope_digest,
        )
        fixed_cells = tuple(
            FixedCellPromptSummary(
                cell_id=cell.result.cell_id,
                window_id=_window_id_for_cell(skeleton, cell.result.cell_id),
                reasons=cell.reasons,
                blocker_refs=cell.blocker_refs,
            )
            for cell in skeleton.fixed_cells
        )
        coverage = SynthesisCoveragePlan(
            planned_cells=skeleton.planned_cell_count,
            supported_cells=skeleton.supported_cell_count,
            fixed_cells=skeleton.fixed_cell_count,
            blocked_cells=skeleton.blocked_cell_count,
            unsupported_cells=skeleton.fixed_cell_count - skeleton.blocked_cell_count,
        )
        operation_id = uuid5(
            NAMESPACE_URL,
            f"synthesis:{skeleton.run_id}:{skeleton.session_id}:"
            f"{skeleton.result_id}:{projection_scope_digest}",
        )
        supported_order = batch_plan.supported_cell_order
        requires_call = bool(supported_order)
        serialized_base_input_bytes = 0
        reserved_result_bytes = 0
        estimated_input_tokens = 0
        estimated_output_tokens = 0
        estimated_output_bytes = 0
        if requires_call:
            placeholders = tuple(
                ValidatedCellPromptSummary(
                    cell_id=cell.cell_id,
                    window_id=cell.window_id,
                    outcome_status=OutcomeStatus.INDETERMINATE,
                    risk_level=RiskLevel.INDETERMINATE,
                    risk_rationale="Reserved validated result slot for sizing.",
                    confidence=0.0,
                    confidence_rationale="Reserved validated result slot for sizing.",
                    evidence_refs=(),
                )
                for batch in batch_plan.batches
                for cell in batch.cells
            )
            prompt = OverallSynthesisPromptInput(
                operation_id=operation_id,
                model=model_fit.model,
                deadline=deadline,
                context=context,
                response_schema=response_schema,
                validated_cells=placeholders,
                fixed_cells=fixed_cells,
            )
            request = self._prompt_builder.build_overall_synthesis(prompt)
            request_bytes = _canonical_bytes(request.model_dump(mode="json"))
            response_shape = _response_shape_bytes(response_schema, supported_order)
            estimate = self._size_estimator.estimate(
                model_fit.model,
                serialized_request=request_bytes,
                serialized_response_shape=response_shape,
            )
            reserved_result_bytes = sum(
                batch.estimated_output_bytes for batch in batch_plan.batches
            )
            reserved_result_tokens = sum(
                batch.estimated_output_tokens for batch in batch_plan.batches
            )
            serialized_base_input_bytes = len(request_bytes)
            estimated_input_tokens = estimate.input_tokens + reserved_result_tokens
            estimated_output_tokens = estimate.output_tokens
            estimated_output_bytes = estimate.estimated_output_bytes
            synthesis_estimate = ModelSizeEstimate(
                input_tokens=estimated_input_tokens,
                output_tokens=estimated_output_tokens,
                estimated_output_bytes=estimated_output_bytes,
            )
            if not self._fits(
                serialized_input_bytes=(
                    serialized_base_input_bytes + reserved_result_bytes
                ),
                estimate=synthesis_estimate,
                model_fit=model_fit,
            ):
                raise ReasoningPlanTooLargeError()

        values = {
            "run_id": skeleton.run_id,
            "session_id": skeleton.session_id,
            "result_id": skeleton.result_id,
            "model": model_fit.model,
            "scope_digest": projection_scope_digest,
            "skeleton_digest": skeleton.skeleton_digest,
            "bundle_ref": skeleton.bundle_ref,
            "bundle_digest": skeleton.bundle_digest,
            "rule_plan_digest": skeleton.rule_plan_digest,
            "capability_registry_digest": skeleton.capability_registry_digest,
            "operation_id": operation_id,
            "deadline": deadline,
            "response_schema": response_schema,
            "context": context,
            "validated_cell_order": supported_order,
            "validated_cell_scopes": tuple(
                cell for batch in batch_plan.batches for cell in batch.cells
            ),
            "fixed_cells": fixed_cells,
            "coverage": coverage,
            "requires_model_call": requires_call,
            "serialized_base_input_bytes": serialized_base_input_bytes,
            "reserved_validated_result_bytes": reserved_result_bytes,
            "estimated_input_tokens": estimated_input_tokens,
            "estimated_output_tokens": estimated_output_tokens,
            "estimated_output_bytes": estimated_output_bytes,
        }
        return SynthesisInputPlan(
            **values,
            synthesis_digest=canonical_json_digest(values),
        )

    def _fits(
        self,
        *,
        serialized_input_bytes: int,
        estimate: ModelSizeEstimate,
        model_fit: ModelFitCapability,
    ) -> bool:
        return (
            serialized_input_bytes <= self._limits.max_serialized_input_bytes
            and estimate.estimated_output_bytes
            <= self._limits.max_serialized_output_bytes
            and estimate.output_tokens <= model_fit.max_output_tokens
            and estimate.input_tokens + estimate.output_tokens
            <= model_fit.context_window_tokens
        )

    @staticmethod
    def _validate_context(
        skeleton: ResultSkeleton,
        context: WorkingContextProjection,
    ) -> None:
        if (
            context.run_id != skeleton.run_id
            or context.session_id != skeleton.session_id
            or context.target_comid != skeleton.target_comid
        ):
            raise ReasoningPlanError("reasoning_context_identity_mismatch")
        if (
            context.bundle_ref != skeleton.bundle_ref
            or context.bundle_digest != skeleton.bundle_digest
            or context.rule_plan_digest != skeleton.rule_plan_digest
            or context.capability_registry_digest
            != skeleton.capability_registry_digest
        ):
            raise ReasoningPlanError("reasoning_context_evidence_mismatch")


def _stable_consecutive_groups(
    skeleton: ResultSkeleton,
    projected_cells: tuple[AgentReasoningCellScope, ...],
) -> tuple[tuple[ParameterKey, int, tuple[AgentReasoningCellScope, ...]], ...]:
    supported = {cell.cell_id: cell for cell in projected_cells}
    groups: list[tuple[ParameterKey, int, tuple[AgentReasoningCellScope, ...]]] = []
    for parameter in skeleton.requested_parameters:
        current: list[AgentReasoningCellScope] = []
        current_segment: int | None = None
        for window in skeleton.windows:
            cell_id = (
                f"cell:{parameter.code_system}:{parameter.code}:{window.window_id}"
            )
            cell = supported.get(cell_id)
            if cell is None or current_segment not in (None, window.segment_index):
                if current:
                    groups.append((parameter, current_segment, tuple(current)))
                    current = []
                current_segment = None
            if cell is not None:
                current_segment = window.segment_index
                current.append(cell)
        if current:
            groups.append((parameter, current_segment, tuple(current)))
    flattened = tuple(cell.cell_id for _, _, cells in groups for cell in cells)
    if flattened != tuple(cell.cell_id for cell in projected_cells):
        raise ReasoningPlanError("reasoning_supported_cell_order_invalid")
    return tuple(groups)


def _batch_context(
    skeleton: ResultSkeleton,
    base: WorkingContextProjection,
    *,
    evidence_summaries: tuple[BoundedEvidenceSummary, ...] | None = None,
    scope_digest: str,
    cells: tuple[AgentReasoningCellScope, ...],
) -> WorkingContextProjection:
    evidence_refs = {ref for cell in cells for ref in cell.allowed_evidence_refs}
    source_allowlist = set(skeleton.source_record_allowlist)
    catalog = base.evidence_summaries if evidence_summaries is None else evidence_summaries
    by_ref = {summary.evidence_ref: summary for summary in catalog}
    if len(by_ref) != len(catalog):
        raise ReasoningPlanError("reasoning_evidence_projection_duplicate")
    required_refs = set(evidence_refs)
    if evidence_summaries is not None:
        pending = list(evidence_refs)
        while pending:
            reference = pending.pop()
            summary = by_ref.get(reference)
            if summary is None:
                raise ReasoningPlanError("reasoning_evidence_projection_incomplete")
            for dependency in summary.dependency_evidence_refs:
                if dependency not in skeleton.evidence_allowlist:
                    raise ReasoningPlanError("reasoning_evidence_projection_out_of_scope")
                if dependency not in required_refs:
                    required_refs.add(dependency)
                    pending.append(dependency)
    summaries = tuple(
        by_ref[reference]
        for reference in sorted(required_refs)
        if reference in by_ref
        and set(by_ref[reference].source_record_item_refs) <= source_allowlist
    )
    if evidence_summaries is not None and len(summaries) != len(required_refs):
        raise ReasoningPlanError("reasoning_evidence_projection_incomplete")
    if len(summaries) > 64:
        raise _BatchContextTooLarge()
    refs = tuple(
        sorted(
            {
                *required_refs,
                *(
                    ref
                    for summary in summaries
                    for ref in summary.source_record_item_refs
                ),
            }
        )
    )
    try:
        return build_working_context_projection(
            run_id=skeleton.run_id,
            session_id=skeleton.session_id,
            target_comid=skeleton.target_comid,
            scope_digest=scope_digest,
            bundle_ref=skeleton.bundle_ref,
            bundle_digest=skeleton.bundle_digest,
            rule_plan_digest=skeleton.rule_plan_digest,
            capability_registry_digest=skeleton.capability_registry_digest,
            reference_allowlist=refs,
            recent_messages=base.recent_messages,
            facts=(),
            evidence_summaries=summaries,
            dependency_context=base.dependency_context,
        )
    except ValidationError as error:
        if any(
            "working context exceeds the bounded byte limit" in item["msg"]
            for item in error.errors()
        ):
            raise _BatchContextTooLarge() from error
        raise


def _synthesis_context(
    skeleton: ResultSkeleton,
    base: WorkingContextProjection,
    *,
    scope_digest: str,
) -> WorkingContextProjection:
    allowlist = tuple(
        sorted({*skeleton.evidence_allowlist, *skeleton.source_record_allowlist})
    )
    allowed = set(allowlist)
    summaries = tuple(
        summary
        for summary in base.evidence_summaries
        if summary.evidence_ref in allowed
        and set(summary.source_record_item_refs) <= allowed
    )
    allowed_kinds = {
        WorkingContextFactKind.COVERAGE,
        WorkingContextFactKind.MISSING,
        WorkingContextFactKind.BLOCKER,
        WorkingContextFactKind.FIXED_CELL,
    }
    base_facts = tuple(
        fact
        for fact in base.facts
        if fact.kind in allowed_kinds and set(fact.refs) <= allowed
    )[:_MAX_SYNTHESIS_BASE_FACTS]
    generated = [
        WorkingContextFact(
            fact_id="m6:coverage:cells",
            kind=WorkingContextFactKind.COVERAGE,
            summary=(
                f"Planned cells={skeleton.planned_cell_count}; "
                f"supported={skeleton.supported_cell_count}; "
                f"fixed={skeleton.fixed_cell_count}."
            ),
        )
    ]
    if skeleton.blocked_cell_count:
        generated.append(
            WorkingContextFact(
                fact_id="m6:coverage:blocked",
                kind=WorkingContextFactKind.BLOCKER,
                summary=f"Technically blocked cells={skeleton.blocked_cell_count}.",
            )
        )
    unsupported = skeleton.fixed_cell_count - skeleton.blocked_cell_count
    if unsupported:
        generated.append(
            WorkingContextFact(
                fact_id="m6:coverage:unsupported",
                kind=WorkingContextFactKind.MISSING,
                summary=f"Naturally unsupported cells={unsupported}.",
            )
        )
    generated_ids = {fact.fact_id for fact in generated}
    facts = tuple(
        fact for fact in base_facts if fact.fact_id not in generated_ids
    ) + tuple(generated)
    return build_working_context_projection(
        run_id=skeleton.run_id,
        session_id=skeleton.session_id,
        target_comid=skeleton.target_comid,
        scope_digest=scope_digest,
        bundle_ref=skeleton.bundle_ref,
        bundle_digest=skeleton.bundle_digest,
        rule_plan_digest=skeleton.rule_plan_digest,
        capability_registry_digest=skeleton.capability_registry_digest,
        reference_allowlist=allowlist,
        recent_messages=(),
        facts=facts,
        evidence_summaries=summaries,
        dependency_context=base.dependency_context,
    )


def _batch_id(
    *,
    batch_index: int,
    run_id: UUID,
    session_id: UUID,
    result_id: str,
    model: ModelSelection,
    scope_digest: str,
    bundle_digest: str,
    rule_plan_digest: str,
    capability_registry_digest: str,
    parameter: ParameterKey,
    segment_index: int,
    cell_ids: tuple[str, ...],
) -> str:
    digest = canonical_json_digest(
        {
            "batch_index": batch_index,
            "run_id": run_id,
            "session_id": session_id,
            "result_id": result_id,
            "model": model,
            "scope_digest": scope_digest,
            "bundle_digest": bundle_digest,
            "rule_plan_digest": rule_plan_digest,
            "capability_registry_digest": capability_registry_digest,
            "parameter": parameter,
            "segment_index": segment_index,
            "cell_ids": cell_ids,
        }
    )
    return f"reasoning-batch:{batch_index:02d}:{digest.removeprefix('sha256:')}"


def _response_shape_bytes(
    schema: WaterbodyJsonSchema,
    cell_ids: tuple[str, ...],
) -> bytes:
    return _canonical_bytes(
        {
            "response_schema": json.loads(schema.schema_text),
            "cell_ids": cell_ids,
        }
    )


def _canonical_bytes(value: object) -> bytes:
    return canonical_json_text(value).encode("utf-8")


def _window_id_for_cell(skeleton: ResultSkeleton, cell_id: str) -> str:
    for parameter in skeleton.requested_parameters:
        prefix = f"cell:{parameter.code_system}:{parameter.code}:"
        if cell_id.startswith(prefix):
            window_id = cell_id.removeprefix(prefix)
            if any(window.window_id == window_id for window in skeleton.windows):
                return window_id
    raise ReasoningPlanError("reasoning_fixed_cell_identity_invalid")
