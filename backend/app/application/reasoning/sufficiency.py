from typing import Self
from uuid import UUID

from pydantic import StrictBool, StrictInt, field_validator, model_validator

from app.application.canonical_json import canonical_json_digest, canonical_json_text
from app.application.evidence_tools import (
    EvidenceAcquisitionHandoff,
    EvidenceProducerStatus,
)
from app.application.reasoning.errors import SealedEvidenceValidationError
from app.application.reasoning.task import ValidatedWaterbodyAgentTask
from app.domain import (
    DomainEnum,
    DomainModel,
    EvidenceCell,
    EvidenceCellRole,
    EvidenceCellStatus,
    EvidenceFragmentReference,
    ItemReference,
    NonEmptyString,
    NumericDerivedPayload,
    ParameterCapabilityRegistry,
    ParameterKey,
    TemporalEstimatePayload,
    TimeRange,
    ensure_unique_tuple,
)


class ReasoningGateDecision(DomainEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"


class SealedEvidenceWindow(DomainModel):
    window_id: NonEmptyString
    time_range: TimeRange
    segment_index: StrictInt

    @model_validator(mode="after")
    def validate_segment_index(self) -> Self:
        if self.segment_index < 0:
            raise ValueError("sealed window segment_index must be non-negative")
        return self


class SealedBudgetUsage(DomainModel):
    collection_used: StrictInt
    collection_limit: StrictInt
    transform_used: StrictInt
    transform_limit: StrictInt

    @model_validator(mode="after")
    def validate_usage(self) -> Self:
        values = (
            self.collection_used,
            self.collection_limit,
            self.transform_used,
            self.transform_limit,
        )
        if any(value < 0 for value in values):
            raise ValueError("sealed evidence budget values must be non-negative")
        if self.collection_used > self.collection_limit:
            raise ValueError("sealed collection usage exceeds its limit")
        if self.transform_used > self.transform_limit:
            raise ValueError("sealed transform usage exceeds its limit")
        return self


class SealedProducerFact(DomainModel):
    name: NonEmptyString
    status: EvidenceProducerStatus
    authorized_for_scope: StrictBool


class SealedPlanningSummary(DomainModel):
    stop_before_model: StrictBool
    stop_facts: tuple[NonEmptyString, ...]
    block_facts: tuple[NonEmptyString, ...]
    fragment_count: StrictInt
    collection_fact_count: StrictInt
    tool_result_count: StrictInt

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if min(
            self.fragment_count,
            self.collection_fact_count,
            self.tool_result_count,
        ) < 0:
            raise ValueError("sealed planning counts must be non-negative")
        ensure_unique_tuple(self.stop_facts, field_name="stop_facts")
        ensure_unique_tuple(self.block_facts, field_name="block_facts")
        return self


class ReasoningCellGate(DomainModel):
    cell_id: NonEmptyString
    parameter: ParameterKey
    window_id: NonEmptyString
    target_status: EvidenceCellStatus
    discharge_status: EvidenceCellStatus
    decision: ReasoningGateDecision
    allowed_evidence_refs: tuple[NonEmptyString, ...]
    contextual_refs: tuple[NonEmptyString, ...]
    collection_fact_refs: tuple[NonEmptyString, ...]
    blocker_refs: tuple[NonEmptyString, ...]
    reasons: tuple[NonEmptyString, ...]

    @model_validator(mode="after")
    def validate_gate(self) -> Self:
        groups = (
            self.allowed_evidence_refs,
            self.contextual_refs,
            self.collection_fact_refs,
            self.blocker_refs,
            self.reasons,
        )
        for values in groups:
            ensure_unique_tuple(values, field_name="reasoning gate values")
        if set(self.allowed_evidence_refs) & (
            set(self.contextual_refs) | set(self.collection_fact_refs)
        ):
            raise ValueError("usable and explanatory gate refs must be disjoint")
        expected = (
            ReasoningGateDecision.BLOCKED
            if self.target_status is EvidenceCellStatus.BLOCKED
            else ReasoningGateDecision.SUPPORTED
            if self.target_status is EvidenceCellStatus.SUPPORTED
            else ReasoningGateDecision.UNSUPPORTED
        )
        if self.decision is not expected:
            raise ValueError("reasoning gate decision must match sealed cell statuses")
        if self.decision is ReasoningGateDecision.SUPPORTED:
            if not self.allowed_evidence_refs or self.reasons or self.blocker_refs:
                raise ValueError("supported gate requires only usable evidence")
        elif not self.reasons:
            raise ValueError("unsupported or blocked gate requires reasons")
        if self.decision is ReasoningGateDecision.BLOCKED and not self.blocker_refs:
            raise ValueError("blocked gate requires technical blocker refs")
        return self


class SealedEvidenceInput(DomainModel):
    schema_version: int = 1
    run_id: UUID
    session_id: UUID
    target_comid: NonEmptyString
    context_ref: ItemReference
    topology_ref: ItemReference
    bundle_ref: ItemReference
    bundle_digest: NonEmptyString
    rule_plan_digest: NonEmptyString
    capability_registry_digest: NonEmptyString
    requested_parameters: tuple[ParameterKey, ...]
    supporting_parameters: tuple[ParameterKey, ...]
    intermediate_parameters: tuple[ParameterKey, ...] = ()
    windows: tuple[SealedEvidenceWindow, ...]
    cells: tuple[EvidenceCell, ...]
    gates: tuple[ReasoningCellGate, ...]
    collection_fact_refs: tuple[EvidenceFragmentReference, ...]
    fragment_allowlist: tuple[EvidenceFragmentReference, ...]
    source_record_allowlist: tuple[NonEmptyString, ...]
    numeric_plan_item_refs: tuple[NonEmptyString, ...]
    tool_result_refs: tuple[ItemReference, ...]
    budget_usage: SealedBudgetUsage
    producer_facts: tuple[SealedProducerFact, ...]
    planning_summary: SealedPlanningSummary
    sealed_digest: NonEmptyString

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be integer 1")
        return value

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        ensure_unique_tuple(self.requested_parameters, field_name="requested_parameters")
        ensure_unique_tuple(self.supporting_parameters, field_name="supporting_parameters")
        ensure_unique_tuple(
            self.intermediate_parameters, field_name="intermediate_parameters"
        )
        all_parameters = (
            *self.requested_parameters,
            *self.supporting_parameters,
            *self.intermediate_parameters,
        )
        if len(set(all_parameters)) != len(all_parameters):
            raise ValueError("sealed parameter roles must be disjoint")
        ensure_unique_tuple(
            tuple(window.window_id for window in self.windows),
            field_name="sealed windows",
        )
        ensure_unique_tuple(
            tuple(reference.evidence_id for reference in self.fragment_allowlist),
            field_name="fragment allowlist",
        )
        ensure_unique_tuple(
            tuple(reference.evidence_id for reference in self.collection_fact_refs),
            field_name="collection fact refs",
        )
        ensure_unique_tuple(
            self.source_record_allowlist,
            field_name="source record allowlist",
        )
        ensure_unique_tuple(
            self.numeric_plan_item_refs, field_name="numeric plan item refs"
        )
        ensure_unique_tuple(
            tuple(reference.item_id for reference in self.tool_result_refs),
            field_name="tool result refs",
        )
        ensure_unique_tuple(
            tuple(fact.name for fact in self.producer_facts),
            field_name="producer facts",
        )
        if self.collection_fact_refs != tuple(
            sorted(
                self.collection_fact_refs,
                key=lambda item: (item.evidence_id, item.item_id),
            )
        ):
            raise ValueError("collection fact refs must use canonical order")
        if self.fragment_allowlist != tuple(
            sorted(
                self.fragment_allowlist,
                key=lambda item: (item.evidence_id, item.item_id),
            )
        ):
            raise ValueError("fragment allowlist must use canonical order")
        if self.source_record_allowlist != tuple(sorted(self.source_record_allowlist)):
            raise ValueError("source record allowlist must use canonical order")
        if self.numeric_plan_item_refs != tuple(sorted(self.numeric_plan_item_refs)):
            raise ValueError("numeric plan item refs must use canonical order")
        if self.tool_result_refs != tuple(
            sorted(self.tool_result_refs, key=lambda item: item.item_id)
        ):
            raise ValueError("tool result refs must use canonical order")
        if self.producer_facts != tuple(
            sorted(self.producer_facts, key=lambda item: item.name)
        ):
            raise ValueError("producer facts must use canonical order")
        if self.planning_summary.stop_facts != tuple(
            sorted(self.planning_summary.stop_facts)
        ) or self.planning_summary.block_facts != tuple(
            sorted(self.planning_summary.block_facts)
        ):
            raise ValueError("planning facts must use canonical order")
        expected_cells = _ordered_cells(
            self.cells,
            requested_parameters=self.requested_parameters,
            supporting_parameters=self.supporting_parameters,
            intermediate_parameters=self.intermediate_parameters,
            window_ids=tuple(window.window_id for window in self.windows),
        )
        if self.cells != expected_cells:
            raise ValueError("sealed cells must use canonical parameter/window order")
        expected_gates = _build_gates(
            self.cells,
            requested_parameters=self.requested_parameters,
            window_ids=tuple(window.window_id for window in self.windows),
        )
        if self.gates != expected_gates:
            raise ValueError("sealed gates must be derived from sealed cells")
        allowlist = {reference.evidence_id for reference in self.fragment_allowlist}
        referenced = {evidence_id for cell in self.cells for evidence_id in cell.fragment_ids()}
        if referenced != allowlist:
            raise ValueError("sealed cells and fragment allowlist must have exact closure")
        expected_collection = {
            evidence_id
            for cell in self.cells
            for evidence_id in cell.collection_fact_refs
        }
        if {
            reference.evidence_id for reference in self.collection_fact_refs
        } != expected_collection:
            raise ValueError("sealed collection facts must be complete")
        if self.planning_summary.fragment_count != len(self.fragment_allowlist):
            raise ValueError("sealed fragment count is not authoritative")
        if self.planning_summary.collection_fact_count != len(
            self.collection_fact_refs
        ):
            raise ValueError("sealed collection fact count is not authoritative")
        if self.planning_summary.tool_result_count != len(self.tool_result_refs):
            raise ValueError("sealed tool result count is not authoritative")
        if self.sealed_digest != _digest_model(self, exclude={"sealed_digest"}):
            raise ValueError("sealed evidence digest does not match canonical content")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_model_bytes(self)


def sealed_transcript_item_refs(
    sealed_input: SealedEvidenceInput,
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                *(reference.item_id for reference in sealed_input.fragment_allowlist),
                *sealed_input.source_record_allowlist,
                *sealed_input.numeric_plan_item_refs,
            }
        )
    )


def seal_evidence_input(
    validated_task: ValidatedWaterbodyAgentTask,
    handoff: EvidenceAcquisitionHandoff,
    capability_registry: ParameterCapabilityRegistry,
) -> SealedEvidenceInput:
    task = validated_task.task
    scope = task.evidence_scope
    bundle = handoff.bundle
    _require(handoff.has_valid_bundle_binding(), "bundle_identity_mismatch")
    _require(bundle.cells == handoff.cells, "handoff_cells_mismatch")
    _require(bundle.requested_parameters == scope.requested_parameters, "bundle_targets_mismatch")
    _require(bundle.supporting_parameters == scope.supporting_parameters, "bundle_support_mismatch")
    _require(
        bundle.intermediate_parameters == scope.intermediate_parameters,
        "bundle_intermediate_mismatch",
    )
    _require(bundle.context_ref == task.analysis_context_ref, "bundle_context_mismatch")
    _require(bundle.topology_ref == task.topology_ref, "bundle_topology_mismatch")
    _require(bundle.agent_comid == task.target_comid, "bundle_comid_mismatch")
    _require(bundle.time_range == validated_task.context.time_range, "bundle_range_mismatch")
    _require(bundle.analysis_timezone == scope.analysis_timezone, "bundle_timezone_mismatch")
    _require(bundle.time_resolution is scope.time_resolution, "bundle_resolution_mismatch")
    _require(
        bundle.time_resolution_source is scope.time_resolution_source,
        "bundle_resolution_source_mismatch",
    )
    _require(
        handoff.rule_plan.plan_digest == handoff.rule_plan_digest,
        "rule_digest_mismatch",
    )
    current_rules = {
        rule.rule_id: rule for rule in handoff.rule_plan.effective_rules
    }
    _require(
        handoff.rule_plan.baseline_rules
        == scope.evidence_rule_plan.baseline_rules
        and all(
            current_rules.get(rule.rule_id) == rule
            for rule in scope.evidence_rule_plan.appended_rules
        ),
        "rule_plan_not_append_only",
    )
    _require(
        handoff.capability_registry_digest == capability_registry.manifest_digest,
        "capability_digest_mismatch",
    )
    _require(handoff.fragment_allowlist == bundle.fragment_refs, "fragment_allowlist_mismatch")
    _require(
        handoff.source_record_allowlist == bundle.source_record_item_refs,
        "source_allowlist_mismatch",
    )
    _require(handoff.tool_result_refs == bundle.tool_result_refs, "tool_result_refs_mismatch")
    collection_ids = {
        evidence_id for cell in bundle.cells for evidence_id in cell.collection_fact_refs
    }
    _require(
        {reference.evidence_id for reference in handoff.collection_fact_refs}
        == collection_ids,
        "collection_fact_refs_mismatch",
    )

    windows = tuple(
        SealedEvidenceWindow(
            window_id=window.window_id,
            time_range=window.time_range,
            segment_index=window.segment_index,
        )
        for window in scope.time_windows
    )
    cells = _ordered_cells(
        handoff.cells,
        requested_parameters=scope.requested_parameters,
        supporting_parameters=scope.supporting_parameters,
        intermediate_parameters=scope.intermediate_parameters,
        window_ids=tuple(window.window_id for window in windows),
    )
    gates = _build_gates(
        cells,
        requested_parameters=scope.requested_parameters,
        window_ids=tuple(window.window_id for window in windows),
    )
    expected_handoff_gates = tuple(
        (
            gate.parameter,
            gate.window_id,
            gate.target_status,
            gate.discharge_status,
            gate.status,
            gate.reasons,
        )
        for gate in handoff.cell_gates
    )
    actual_gates = tuple(
        (
            gate.parameter,
            gate.window_id,
            gate.target_status,
            gate.discharge_status,
            EvidenceCellStatus(gate.decision.value),
            gate.reasons,
        )
        for gate in gates
    )
    _require(
        len(expected_handoff_gates) == len(actual_gates)
        and set(expected_handoff_gates) == set(actual_gates),
        "handoff_gate_mismatch",
    )

    numeric_plan_item_refs = tuple(sorted({
        fragment.payload.plan_item_ref.item_id
        for _reference, fragment in handoff.validated_inputs.fragments
        if isinstance(
            fragment.payload,
            (NumericDerivedPayload, TemporalEstimatePayload),
        )
        and fragment.payload.plan_item_ref is not None
    }))
    _require(
        set(numeric_plan_item_refs)
        <= {reference.item_id for reference in bundle.closure_refs},
        "numeric_plan_closure_mismatch",
    )
    values = {
        "schema_version": 1,
        "run_id": task.run_id,
        "session_id": task.session_id,
        "target_comid": task.target_comid,
        "context_ref": task.analysis_context_ref,
        "topology_ref": task.topology_ref,
        "bundle_ref": handoff.bundle_ref,
        "bundle_digest": bundle.bundle_id,
        "rule_plan_digest": handoff.rule_plan_digest,
        "capability_registry_digest": handoff.capability_registry_digest,
        "requested_parameters": scope.requested_parameters,
        "supporting_parameters": scope.supporting_parameters,
        "intermediate_parameters": scope.intermediate_parameters,
        "windows": windows,
        "cells": cells,
        "gates": gates,
        "collection_fact_refs": tuple(
            sorted(
                handoff.collection_fact_refs,
                key=lambda item: (item.evidence_id, item.item_id),
            )
        ),
        "fragment_allowlist": tuple(
            sorted(
                handoff.fragment_allowlist,
                key=lambda item: (item.evidence_id, item.item_id),
            )
        ),
        "source_record_allowlist": tuple(sorted(handoff.source_record_allowlist)),
        "numeric_plan_item_refs": numeric_plan_item_refs,
        "tool_result_refs": tuple(
            sorted(handoff.tool_result_refs, key=lambda item: item.item_id)
        ),
        "budget_usage": SealedBudgetUsage(
            collection_used=handoff.budget_usage.collection_used,
            collection_limit=handoff.budget_usage.collection_limit,
            transform_used=handoff.budget_usage.transform_used,
            transform_limit=handoff.budget_usage.transform_limit,
        ),
        "producer_facts": tuple(
            SealedProducerFact(
                name=fact.name,
                status=fact.status,
                authorized_for_scope=fact.authorized_for_scope,
            )
            for fact in sorted(handoff.producer_facts, key=lambda item: item.name)
        ),
        "planning_summary": SealedPlanningSummary(
            stop_before_model=handoff.stop_before_model,
            stop_facts=tuple(sorted(set(handoff.stop_facts))),
            block_facts=tuple(sorted(set(handoff.block_facts))),
            fragment_count=len(handoff.fragment_allowlist),
            collection_fact_count=len(handoff.collection_fact_refs),
            tool_result_count=len(handoff.tool_result_refs),
        ),
    }
    digest = _digest_values(values)
    return SealedEvidenceInput(**values, sealed_digest=digest)


def validate_sealed_evidence_input(value: SealedEvidenceInput) -> SealedEvidenceInput:
    return SealedEvidenceInput.model_validate(value.model_dump(mode="python"))


def _ordered_cells(
    cells: tuple[EvidenceCell, ...],
    *,
    requested_parameters: tuple[ParameterKey, ...],
    supporting_parameters: tuple[ParameterKey, ...],
    intermediate_parameters: tuple[ParameterKey, ...],
    window_ids: tuple[str, ...],
) -> tuple[EvidenceCell, ...]:
    by_identity = {
        (cell.parameter, cell.window_id, cell.role): cell for cell in cells
    }
    if len(by_identity) != len(cells):
        raise ValueError("sealed cells must have unique identities")
    expected = tuple(
        (parameter, window_id, role)
        for role, parameters in (
            (EvidenceCellRole.TARGET, requested_parameters),
            (EvidenceCellRole.REQUIRED_SUPPORT, supporting_parameters),
            (EvidenceCellRole.METHOD_INPUT, intermediate_parameters),
        )
        for parameter in parameters
        for window_id in window_ids
    )
    if set(by_identity) != set(expected):
        raise ValueError("sealed cells must exactly cover the evidence scope")
    return tuple(by_identity[identity] for identity in expected)


def _build_gates(
    cells: tuple[EvidenceCell, ...],
    *,
    requested_parameters: tuple[ParameterKey, ...],
    window_ids: tuple[str, ...],
) -> tuple[ReasoningCellGate, ...]:
    discharge = ParameterKey(code_system="usgs", code="00060")
    by_identity = {
        (cell.parameter, cell.window_id, cell.role): cell for cell in cells
    }
    discharge_role = (
        EvidenceCellRole.TARGET
        if discharge in requested_parameters else EvidenceCellRole.REQUIRED_SUPPORT
    )
    gates = []
    for parameter in requested_parameters:
        for window_id in window_ids:
            target = by_identity[(parameter, window_id, EvidenceCellRole.TARGET)]
            support = by_identity[
                (discharge, window_id, discharge_role)
            ]
            decision = (
                ReasoningGateDecision.BLOCKED
                if target.status is EvidenceCellStatus.BLOCKED
                else ReasoningGateDecision.SUPPORTED
                if target.status is EvidenceCellStatus.SUPPORTED
                else ReasoningGateDecision.UNSUPPORTED
            )
            reasons = []
            if target.status is not EvidenceCellStatus.SUPPORTED:
                reasons.extend(f"target:{reason}" for reason in target.reasons)
            if (decision is not ReasoningGateDecision.SUPPORTED
                    and support is not target and support.status is not EvidenceCellStatus.SUPPORTED):
                reasons.extend(
                    f"required_discharge:{reason}" for reason in support.reasons
                )
            allowed = _unique_sorted(
                target.direct_fragment_refs
                + target.allowed_indirect_fragment_refs
                + support.direct_fragment_refs
                + support.allowed_indirect_fragment_refs
            )
            contextual = _unique_sorted(
                target.contextual_fragment_refs + support.contextual_fragment_refs
            )
            contextual = tuple(ref for ref in contextual if ref not in allowed)
            facts = _unique_sorted(
                target.collection_fact_refs + support.collection_fact_refs
            )
            blocker_refs = (
                _unique_sorted(
                    (
                        target.collection_fact_refs
                        if target.status is EvidenceCellStatus.BLOCKED
                        else ()
                    )
                    + (
                        support.collection_fact_refs
                        if support.status is EvidenceCellStatus.BLOCKED
                        else ()
                    )
                )
                if decision is ReasoningGateDecision.BLOCKED
                else ()
            )
            gates.append(
                ReasoningCellGate(
                    cell_id=_cell_id(parameter, window_id),
                    parameter=parameter,
                    window_id=window_id,
                    target_status=target.status,
                    discharge_status=support.status,
                    decision=decision,
                    allowed_evidence_refs=allowed,
                    contextual_refs=contextual,
                    collection_fact_refs=facts,
                    blocker_refs=blocker_refs,
                    reasons=tuple(dict.fromkeys(reasons)),
                )
            )
    return tuple(gates)


def _cell_id(parameter: ParameterKey, window_id: str) -> str:
    return f"cell:{parameter.code_system}:{parameter.code}:{window_id}"


def _unique_sorted(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise SealedEvidenceValidationError(code)


def _canonical_model_bytes(model: DomainModel) -> bytes:
    return canonical_json_text(model).encode("utf-8")


def _digest_model(model: DomainModel, *, exclude: set[str]) -> str:
    return canonical_json_digest(model.model_dump(mode="json", exclude=exclude))


def _digest_values(values: object) -> str:
    return canonical_json_digest(values)
