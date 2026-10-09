import json
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from typing import Self
from uuid import UUID

from pydantic import StrictInt, field_validator, model_validator

from app.application.reasoning.errors import (
    AllCellsBlockedError,
    ResultSkeletonValidationError,
)
from app.application.reasoning.sufficiency import (
    ReasoningCellGate,
    ReasoningGateDecision,
    SealedEvidenceInput,
    validate_sealed_evidence_input,
)
from app.application.reasoning.task import ValidatedWaterbodyAgentTask
from app.domain.settings_admin import UnitPreference
from app.domain import (
    AggregationMethod,
    DomainModel,
    IndeterminateReason,
    InferenceMode,
    ItemReference,
    NonEmptyString,
    OutcomeStatus,
    ParameterCapabilityRegistry,
    ParameterKey,
    ResultSource,
    RiskLevel,
    RiskOrTrendAssessment,
    TimeRange,
    TimeResolution,
    TimeResolutionSource,
    TimeSegment,
    TimeWindowResult,
    UtcDatetime,
    ensure_unique_tuple,
)


class ResultSkeletonWindow(DomainModel):
    window_id: NonEmptyString
    start_time: UtcDatetime
    end_time: UtcDatetime
    segment_index: StrictInt
    mode: InferenceMode

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.segment_index < 0:
            raise ValueError("segment_index must be non-negative")
        if self.start_time >= self.end_time:
            raise ValueError("skeleton window start must precede end")
        if self.mode is InferenceMode.MIXED:
            raise ValueError("skeleton window cannot use mixed mode")
        return self


class SupportedCellScope(DomainModel):
    cell_id: NonEmptyString
    parameter: ParameterKey
    window_id: NonEmptyString
    allowed_evidence_refs: tuple[NonEmptyString, ...]
    allowed_result_sources: tuple[ResultSource, ...]
    allowed_unit_ids: tuple[NonEmptyString, ...]
    preferred_target_unit_id: NonEmptyString | None = None
    unit_preference_source: str | None = None
    unit_registry_digest: str | None = None
    allowed_aggregation_methods: tuple[AggregationMethod, ...]

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if not self.allowed_evidence_refs:
            raise ValueError("supported cell requires an evidence allowlist")
        if not self.allowed_result_sources:
            raise ValueError("supported cell requires result-source eligibility")
        if not self.allowed_unit_ids:
            raise ValueError("supported cell requires unit eligibility")
        for values in (
            self.allowed_evidence_refs,
            self.allowed_result_sources,
            self.allowed_unit_ids,
            self.allowed_aggregation_methods,
        ):
            ensure_unique_tuple(values, field_name="supported cell eligibility")
        return self


class FixedSkeletonCell(DomainModel):
    decision: ReasoningGateDecision
    result: TimeWindowResult
    reasons: tuple[NonEmptyString, ...]
    blocker_refs: tuple[NonEmptyString, ...]

    @model_validator(mode="after")
    def validate_fixed_cell(self) -> Self:
        if self.decision is ReasoningGateDecision.SUPPORTED:
            raise ValueError("supported cells cannot be fixed by the backend")
        if not self.reasons:
            raise ValueError("fixed cell requires gate reasons")
        ensure_unique_tuple(self.reasons, field_name="fixed cell reasons")
        ensure_unique_tuple(self.blocker_refs, field_name="fixed blocker refs")
        if self.decision is ReasoningGateDecision.BLOCKED:
            if not self.blocker_refs:
                raise ValueError("blocked fixed cell requires blocker refs")
            if not set(self.blocker_refs) <= set(self.result.evidence_refs):
                raise ValueError("fixed blocker refs must be included in evidence refs")
            if self.result.assessment.reason_codes != (
                IndeterminateReason.SOURCE_UNAVAILABLE,
            ):
                raise ValueError("blocked fixed cell requires source_unavailable")
        elif self.blocker_refs:
            raise ValueError("natural unsupported cell cannot contain blocker refs")
        return self


class AgentReasoningCellScope(DomainModel):
    cell_id: NonEmptyString
    parameter: ParameterKey
    window_id: NonEmptyString
    start_time: UtcDatetime
    end_time: UtcDatetime
    mode: InferenceMode
    allowed_evidence_refs: tuple[NonEmptyString, ...]
    allowed_result_sources: tuple[ResultSource, ...]
    allowed_unit_ids: tuple[NonEmptyString, ...]
    preferred_target_unit_id: NonEmptyString | None = None
    unit_preference_source: str | None = None
    unit_registry_digest: str | None = None
    allowed_aggregation_methods: tuple[AggregationMethod, ...]


class ReasoningSkeletonProjection(DomainModel):
    scope_digest: NonEmptyString
    cells: tuple[AgentReasoningCellScope, ...]

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        expected = _digest_values(self.model_dump(mode="json", exclude={"scope_digest"}))
        if self.scope_digest != expected:
            raise ValueError("reasoning projection digest does not match its cells")
        return self


class ReasoningCellClaim(DomainModel):
    cell_id: NonEmptyString
    parameter: ParameterKey
    window_id: NonEmptyString
    evidence_refs: tuple[NonEmptyString, ...]

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return ensure_unique_tuple(value, field_name="claim evidence_refs")


class ResultSkeleton(DomainModel):
    schema_version: int = 1
    result_id: NonEmptyString
    run_id: UUID
    session_id: UUID
    target_comid: NonEmptyString
    mode: InferenceMode
    time_range: TimeRange
    time_segments: tuple[TimeSegment, ...]
    analysis_timezone: NonEmptyString
    time_resolution: TimeResolution
    time_resolution_source: TimeResolutionSource
    requested_parameters: tuple[ParameterKey, ...]
    unit_preferences: tuple[UnitPreference, ...] = ()
    windows: tuple[ResultSkeletonWindow, ...]
    cell_order: tuple[NonEmptyString, ...]
    fixed_cells: tuple[FixedSkeletonCell, ...]
    supported_scopes: tuple[SupportedCellScope, ...]
    evidence_allowlist: tuple[NonEmptyString, ...]
    source_record_allowlist: tuple[NonEmptyString, ...]
    context_ref: ItemReference
    topology_ref: ItemReference
    bundle_ref: ItemReference
    bundle_digest: NonEmptyString
    rule_plan_digest: NonEmptyString
    capability_registry_digest: NonEmptyString
    planned_cell_count: StrictInt
    fixed_cell_count: StrictInt
    supported_cell_count: StrictInt
    blocked_cell_count: StrictInt
    generated_at: UtcDatetime
    skeleton_digest: NonEmptyString

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be integer 1")
        return value

    @model_validator(mode="after")
    def validate_skeleton(self) -> Self:
        ensure_unique_tuple(self.requested_parameters, field_name="requested_parameters")
        ensure_unique_tuple(self.cell_order, field_name="cell_order")
        ensure_unique_tuple(
            tuple(window.window_id for window in self.windows),
            field_name="skeleton windows",
        )
        if tuple(window.start_time for window in self.windows) != tuple(
            sorted(window.start_time for window in self.windows)
        ):
            raise ValueError("skeleton windows must use chronological order")
        if any(
            window.segment_index >= len(self.time_segments)
            or window.mode is not self.time_segments[window.segment_index].mode
            or window.start_time < self.time_segments[window.segment_index].start
            or window.end_time > self.time_segments[window.segment_index].end
            for window in self.windows
        ):
            raise ValueError("skeleton windows must match their time segments")
        ensure_unique_tuple(self.evidence_allowlist, field_name="evidence_allowlist")
        ensure_unique_tuple(
            self.source_record_allowlist,
            field_name="source_record_allowlist",
        )
        if self.evidence_allowlist != tuple(sorted(self.evidence_allowlist)):
            raise ValueError("evidence_allowlist must use canonical order")
        if self.source_record_allowlist != tuple(sorted(self.source_record_allowlist)):
            raise ValueError("source_record_allowlist must use canonical order")
        fixed_ids = tuple(cell.result.cell_id for cell in self.fixed_cells)
        supported_ids = tuple(cell.cell_id for cell in self.supported_scopes)
        ensure_unique_tuple(fixed_ids, field_name="fixed cell IDs")
        ensure_unique_tuple(supported_ids, field_name="supported cell IDs")
        if set(fixed_ids) & set(supported_ids):
            raise ValueError("fixed and supported cell identities must be disjoint")
        expected_cell_order = tuple(
            _cell_id(parameter, window.window_id)
            for parameter in self.requested_parameters
            for window in self.windows
        )
        if self.cell_order != expected_cell_order:
            raise ValueError("cell_order must exactly cover parameter/window order")
        ensure_unique_tuple(
            tuple(item.parameter for item in self.unit_preferences),
            field_name="skeleton unit preferences",
        )
        if any(
            item.registry_digest != self.capability_registry_digest
            for item in self.unit_preferences
        ):
            raise ValueError("skeleton unit preferences must match the sealed registry")
        if set((*fixed_ids, *supported_ids)) != set(self.cell_order):
            raise ValueError("fixed and supported cells must exactly cover cell_order")
        if fixed_ids != tuple(
            cell_id for cell_id in self.cell_order if cell_id in set(fixed_ids)
        ) or supported_ids != tuple(
            cell_id for cell_id in self.cell_order if cell_id in set(supported_ids)
        ):
            raise ValueError("fixed and supported cells must preserve cell_order")
        windows_by_id = {window.window_id: window for window in self.windows}
        for scope in self.supported_scopes:
            if (
                scope.window_id not in windows_by_id
                or scope.cell_id != _cell_id(scope.parameter, scope.window_id)
                or scope.parameter not in self.requested_parameters
            ):
                raise ValueError("supported scope identity is outside the skeleton")
            preference = next(
                (item for item in self.unit_preferences if item.parameter == scope.parameter),
                None,
            )
            if preference is None and scope.preferred_target_unit_id is not None:
                raise ValueError("unbound unit preference in supported scope")
            if preference is not None and (
                scope.preferred_target_unit_id != preference.target_unit_id
                or scope.unit_preference_source != preference.source
                or scope.unit_registry_digest != preference.registry_digest
            ):
                raise ValueError("supported scope unit preference differs from context")
        bounds_by_cell = {
            _cell_id(parameter, window.window_id): (
                window.start_time,
                window.end_time,
            )
            for parameter in self.requested_parameters
            for window in self.windows
        }
        if any(
            (
                cell.result.start_time,
                cell.result.end_time,
            )
            != bounds_by_cell.get(cell.result.cell_id)
            for cell in self.fixed_cells
        ):
            raise ValueError("fixed cell bounds must match skeleton windows")
        if self.planned_cell_count != len(self.cell_order):
            raise ValueError("planned_cell_count is not authoritative")
        if self.fixed_cell_count != len(self.fixed_cells):
            raise ValueError("fixed_cell_count is not authoritative")
        if self.supported_cell_count != len(self.supported_scopes):
            raise ValueError("supported_cell_count is not authoritative")
        if self.blocked_cell_count != sum(
            cell.decision is ReasoningGateDecision.BLOCKED
            for cell in self.fixed_cells
        ):
            raise ValueError("blocked_cell_count is not authoritative")
        allowlist = set(self.evidence_allowlist)
        for cell in self.fixed_cells:
            if not set(cell.result.evidence_refs) <= allowlist:
                raise ValueError("fixed cell references evidence outside allowlist")
        for scope in self.supported_scopes:
            if not set(scope.allowed_evidence_refs) <= allowlist:
                raise ValueError("supported cell references evidence outside allowlist")
        if self.skeleton_digest != _digest_model(self, exclude={"skeleton_digest"}):
            raise ValueError("result skeleton digest does not match canonical content")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_model_bytes(self)

    def agent_projection(self) -> ReasoningSkeletonProjection:
        windows = {window.window_id: window for window in self.windows}
        cells = tuple(
            AgentReasoningCellScope(
                cell_id=scope.cell_id,
                parameter=scope.parameter,
                window_id=scope.window_id,
                start_time=windows[scope.window_id].start_time,
                end_time=windows[scope.window_id].end_time,
                mode=windows[scope.window_id].mode,
                allowed_evidence_refs=scope.allowed_evidence_refs,
                allowed_result_sources=scope.allowed_result_sources,
                allowed_unit_ids=scope.allowed_unit_ids,
                preferred_target_unit_id=scope.preferred_target_unit_id,
                unit_preference_source=scope.unit_preference_source,
                unit_registry_digest=scope.unit_registry_digest,
                allowed_aggregation_methods=scope.allowed_aggregation_methods,
            )
            for scope in self.supported_scopes
        )
        digest = _digest_values(
            {"cells": [cell.model_dump(mode="json") for cell in cells]}
        )
        return ReasoningSkeletonProjection(scope_digest=digest, cells=cells)


def build_result_skeleton(
    validated_task: ValidatedWaterbodyAgentTask,
    sealed_input: SealedEvidenceInput,
    capability_registry: ParameterCapabilityRegistry,
    *,
    result_id: str,
    generated_at,
) -> ResultSkeleton:
    sealed = validate_sealed_evidence_input(sealed_input)
    task = validated_task.task
    context = validated_task.context
    _require(sealed.run_id == task.run_id, "skeleton_run_mismatch")
    _require(sealed.session_id == task.session_id, "skeleton_session_mismatch")
    _require(sealed.target_comid == task.target_comid, "skeleton_comid_mismatch")
    _require(
        sealed.capability_registry_digest == capability_registry.manifest_digest,
        "skeleton_capability_digest_mismatch",
    )
    if sealed.gates and all(
        gate.decision is ReasoningGateDecision.BLOCKED for gate in sealed.gates
    ):
        raise AllCellsBlockedError()

    segments = tuple(
        TimeSegment(
            start=segment.time_range.start,
            end=segment.time_range.end,
            mode=segment.mode,
        )
        for segment in context.time_segments
    )
    windows = tuple(
        ResultSkeletonWindow(
            window_id=window.window_id,
            start_time=window.time_range.start,
            end_time=window.time_range.end,
            segment_index=window.segment_index,
            mode=context.time_segments[window.segment_index].mode,
        )
        for window in sealed.windows
    )
    window_by_id = {window.window_id: window for window in windows}
    target_cells = {
        (cell.parameter, cell.window_id): cell
        for cell in sealed.cells
        if cell.role.value == "target"
    }
    fixed_cells = []
    supported_scopes = []
    preferences = {item.parameter: item for item in context.unit_preferences}
    for gate in sealed.gates:
        window = window_by_id[gate.window_id]
        if gate.decision is ReasoningGateDecision.SUPPORTED:
            capability = capability_registry.get(gate.parameter)
            _require(capability is not None, "skeleton_parameter_not_executable")
            target = target_cells[(gate.parameter, gate.window_id)]
            preference = preferences.get(gate.parameter)
            preferred_unit = (
                capability_registry.unit_definitions[preference.target_unit_id]
                if preference is not None else None
            )
            supported_scopes.append(
                SupportedCellScope(
                    cell_id=gate.cell_id,
                    parameter=gate.parameter,
                    window_id=gate.window_id,
                    allowed_evidence_refs=gate.allowed_evidence_refs,
                    allowed_result_sources=_allowed_sources(window.mode, target),
                    allowed_unit_ids=tuple(dict.fromkeys((
                        capability.canonical_unit,
                        *((preferred_unit.symbol,) if preferred_unit is not None else ()),
                    ))),
                    preferred_target_unit_id=(
                        preference.target_unit_id if preference is not None else None
                    ),
                    unit_preference_source=(
                        preference.source if preference is not None else None
                    ),
                    unit_registry_digest=(
                        preference.registry_digest if preference is not None else None
                    ),
                    allowed_aggregation_methods=_allowed_aggregations(
                        capability_registry,
                        capability,
                    ),
                )
            )
        else:
            fixed_cells.append(_fixed_cell(gate, window))

    values = {
        "schema_version": 1,
        "result_id": result_id,
        "run_id": task.run_id,
        "session_id": task.session_id,
        "target_comid": task.target_comid,
        "mode": context.mode,
        "time_range": context.time_range,
        "time_segments": segments,
        "analysis_timezone": context.analysis_timezone,
        "time_resolution": context.time_resolution,
        "time_resolution_source": context.time_resolution_source,
        "requested_parameters": sealed.requested_parameters,
        "unit_preferences": context.unit_preferences,
        "windows": windows,
        "cell_order": tuple(gate.cell_id for gate in sealed.gates),
        "fixed_cells": tuple(fixed_cells),
        "supported_scopes": tuple(supported_scopes),
        "evidence_allowlist": tuple(
            reference.evidence_id for reference in sealed.fragment_allowlist
        ),
        "source_record_allowlist": sealed.source_record_allowlist,
        "context_ref": sealed.context_ref,
        "topology_ref": sealed.topology_ref,
        "bundle_ref": sealed.bundle_ref,
        "bundle_digest": sealed.bundle_digest,
        "rule_plan_digest": sealed.rule_plan_digest,
        "capability_registry_digest": sealed.capability_registry_digest,
        "planned_cell_count": len(sealed.gates),
        "fixed_cell_count": len(fixed_cells),
        "supported_cell_count": len(supported_scopes),
        "blocked_cell_count": sum(
            gate.decision is ReasoningGateDecision.BLOCKED for gate in sealed.gates
        ),
        "generated_at": generated_at,
    }
    return ResultSkeleton(
        **values,
        skeleton_digest=_digest_values(values),
    )


def validate_result_skeleton(value: ResultSkeleton) -> ResultSkeleton:
    return ResultSkeleton.model_validate(value.model_dump(mode="python"))


def validate_agent_cell_claims(
    skeleton: ResultSkeleton,
    claims: tuple[ReasoningCellClaim | dict, ...],
) -> tuple[ReasoningCellClaim, ...]:
    validated = tuple(ReasoningCellClaim.model_validate(claim) for claim in claims)
    by_id = {scope.cell_id: scope for scope in skeleton.supported_scopes}
    if len({claim.cell_id for claim in validated}) != len(validated):
        raise ResultSkeletonValidationError("duplicate_reasoning_cell_claim")
    for claim in validated:
        scope = by_id.get(claim.cell_id)
        if scope is None:
            raise ResultSkeletonValidationError("reasoning_cell_not_supported")
        if claim.parameter != scope.parameter or claim.window_id != scope.window_id:
            raise ResultSkeletonValidationError("reasoning_cell_identity_mismatch")
        if not set(claim.evidence_refs) <= set(scope.allowed_evidence_refs):
            raise ResultSkeletonValidationError("reasoning_evidence_out_of_scope")
    return validated


def _fixed_cell(
    gate: ReasoningCellGate,
    window: ResultSkeletonWindow,
) -> FixedSkeletonCell:
    blocked = gate.decision is ReasoningGateDecision.BLOCKED
    evidence_refs = (
        gate.blocker_refs
        if blocked
        else tuple(
            dict.fromkeys((*gate.collection_fact_refs, *gate.contextual_refs))
        )
    )
    if not evidence_refs:
        raise ResultSkeletonValidationError("fixed_cell_missing_fact_reference")
    reason = (
        IndeterminateReason.SOURCE_UNAVAILABLE
        if blocked
        else IndeterminateReason.INSUFFICIENT_EVIDENCE
    )
    rationale = (
        "Required evidence collection was technically blocked."
        if blocked
        else "No direct or materialized allowed-indirect evidence supports this cell."
    )
    result = TimeWindowResult(
        cell_id=gate.cell_id,
        start_time=window.start_time,
        end_time=window.end_time,
        outcome_status=OutcomeStatus.INDETERMINATE,
        representative_value=None,
        unit_id=None,
        assessment=RiskOrTrendAssessment(
            summary=rationale,
            trend=None,
            risk_level=RiskLevel.INDETERMINATE,
            risk_rationale=rationale,
            reason_codes=(reason,),
            supporting_refs=evidence_refs,
        ),
        source=None,
        confidence=0,
        confidence_rationale=rationale,
        aggregation_method=None,
        evidence_refs=evidence_refs,
    )
    return FixedSkeletonCell(
        decision=gate.decision,
        result=result,
        reasons=gate.reasons,
        blocker_refs=gate.blocker_refs,
    )


def _cell_id(parameter: ParameterKey, window_id: str) -> str:
    return f"cell:{parameter.code_system}:{parameter.code}:{window_id}"


def _allowed_sources(mode: InferenceMode, target_cell) -> tuple[ResultSource, ...]:
    if mode is InferenceMode.PREDICTION:
        return (ResultSource.PREDICTED,)
    sources = []
    if target_cell.direct_fragment_refs:
        sources.extend((ResultSource.OBSERVED, ResultSource.INFERRED))
    if target_cell.allowed_indirect_fragment_refs:
        sources.append(ResultSource.INFERRED)
    return tuple(dict.fromkeys(sources))


def _allowed_aggregations(registry, capability) -> tuple[AggregationMethod, ...]:
    methods = []
    for policy in capability.channel_policies:
        profile = registry.aggregation_profiles[policy.aggregation_profile_id]
        for method in profile.allowed_methods:
            try:
                methods.append(AggregationMethod(method.value))
            except ValueError:
                continue
    return tuple(sorted(set(methods), key=lambda item: item.value))


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ResultSkeletonValidationError(code)


def _canonical_model_bytes(model: DomainModel) -> bytes:
    return json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest_model(model: DomainModel, *, exclude: set[str]) -> str:
    return _digest_values(model.model_dump(mode="json", exclude=exclude))


def _digest_values(values: object) -> str:
    payload = json.dumps(
        _json_value(values),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{sha256(payload).hexdigest()}"


def _json_value(value):
    if isinstance(value, DomainModel):
        return value.model_dump(mode="json")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value
