import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from app.application.reasoning import (
    FixedSkeletonCell,
    ModelFitCapability,
    ModelSizeEstimate,
    ReasoningBatchPlanner,
    ReasoningGateDecision,
    ReasoningPlanError,
    ReasoningPlannerLimits,
    ReasoningPlanTooLargeError,
    ResultSkeleton,
    ResultSkeletonWindow,
    SupportedCellScope,
    build_result_skeleton,
    seal_evidence_input,
)
from app.application.reasoning.batch_planner import _synthesis_context
from app.application.reasoning.model_models import WaterbodyJsonSchema
from app.application.reasoning.planning_loop import (
    _working_context as m5_working_context,
)
from app.application.reasoning.prompt_builder import (
    PromptBuilder,
    ValidatedCellPromptSummary,
)
from app.application.reasoning.prompt_policy import (
    DEFAULT_REASONING_PROMPT_POLICY,
    canonical_json_digest,
    canonical_json_text,
)
from app.application.reasoning.working_context import (
    BoundedEvidenceSummary,
    WorkingContextFact,
    WorkingContextFactKind,
    build_working_context_projection,
)
from app.domain import (
    AggregationMethod,
    IndeterminateReason,
    InferenceMode,
    ItemReference,
    ModelCapability,
    ModelCatalogSnapshot,
    ModelRole,
    ModelSelection,
    OutcomeStatus,
    ParameterKey,
    ResultSource,
    RiskLevel,
    RiskOrTrendAssessment,
    TimeRange,
    TimeResolution,
    TimeResolutionSource,
    TimeSegment,
    TimeWindowResult,
    build_time_windows,
)
from pydantic import ValidationError
from tests.application.reasoning.test_task_and_skeleton import (
    NOW,
    _validated_fixture,
)
from tests.m4_support import EMBEDDING_CAPABILITY

RUN_ID = UUID("10000000-0000-4000-8000-000000000001")
SESSION_ID = UUID("20000000-0000-4000-8000-000000000001")
MODEL = ModelSelection(provider="test-provider", model="waterbody-model")
DEADLINE = datetime(2026, 10, 2, 12, tzinfo=UTC)
CATALOG = ModelCatalogSnapshot(
    capabilities=(
        ModelCapability(
            provider=MODEL.provider,
            model=MODEL.model,
            roles=(ModelRole.WATERBODY,),
            context_window_tokens=10_000_000,
            max_output_tokens=1_000_000,
        ),
        EMBEDDING_CAPABILITY,
    )
)


@dataclass(frozen=True)
class ExactByteEstimator:
    output_tokens_per_cell: int = 8
    output_bytes_per_cell: int = 64

    def estimate(
        self,
        model,
        *,
        serialized_request: bytes,
        serialized_response_shape: bytes,
    ) -> ModelSizeEstimate:
        shape = json.loads(serialized_response_shape)
        cell_count = len(shape["cell_ids"])
        return ModelSizeEstimate(
            input_tokens=ceil(len(serialized_request) / 4),
            output_tokens=(
                ceil(len(serialized_response_shape) / 4)
                + cell_count * self.output_tokens_per_cell
            ),
            estimated_output_bytes=(
                len(serialized_response_shape)
                + cell_count * self.output_bytes_per_cell
            ),
        )


def _schema(name: str, *, reverse: bool = False) -> WaterbodyJsonSchema:
    properties = {
        "scope_digest": {"type": "string"},
        "cells": {"type": "array", "items": {"type": "object"}},
    }
    if reverse:
        properties = dict(reversed(tuple(properties.items())))
    return WaterbodyJsonSchema(
        name=name,
        schema_text=json.dumps(
            {
                "type": "object",
                "properties": properties,
                "required": ["scope_digest", "cells"],
                "additionalProperties": False,
            }
        ),
    )


def _local_range(
    start: datetime,
    end: datetime,
    timezone_name: str = "America/Chicago",
) -> TimeRange:
    zone = ZoneInfo(timezone_name)
    return TimeRange(start=start.replace(tzinfo=zone), end=end.replace(tzinfo=zone))


def _parameters(count: int) -> tuple[ParameterKey, ...]:
    return tuple(
        ParameterKey(code_system="test", code=f"p{index}")
        for index in range(count)
    )


def _make_skeleton(
    *,
    parameter_count: int = 1,
    resolution: TimeResolution = TimeResolution.SIX_HOURS,
    segment_specs: tuple[tuple[TimeRange, InferenceMode], ...] | None = None,
    supported=lambda _parameter_index, _window_index: True,
    blocked=lambda _parameter_index, _window_index: False,
    result_id: str = "result-1",
) -> ResultSkeleton:
    if segment_specs is None:
        segment_specs = (
            (
                _local_range(datetime(2026, 2, 1), datetime(2026, 2, 2)),
                InferenceMode.GAP_FILLING,
            ),
        )
    parameters = _parameters(parameter_count)
    segments = tuple(
        TimeSegment(start=time_range.start, end=time_range.end, mode=mode)
        for time_range, mode in segment_specs
    )
    windows: list[ResultSkeletonWindow] = []
    for segment_index, (time_range, mode) in enumerate(segment_specs):
        for window in build_time_windows(
            time_range,
            resolution,
            "America/Chicago",
        ):
            window_index = len(windows)
            windows.append(
                ResultSkeletonWindow(
                    window_id=f"w{window_index:03d}",
                    start_time=window.start,
                    end_time=window.end,
                    segment_index=segment_index,
                    mode=mode,
                )
            )

    cell_order: list[str] = []
    fixed_cells: list[FixedSkeletonCell] = []
    supported_scopes: list[SupportedCellScope] = []
    evidence_refs: list[str] = []
    source_refs: list[str] = []
    blocked_count = 0
    for parameter_index, parameter in enumerate(parameters):
        for window_index, window in enumerate(windows):
            cell_id = (
                f"cell:{parameter.code_system}:{parameter.code}:{window.window_id}"
            )
            evidence_ref = f"e:{parameter_index}:{window_index}"
            source_ref = f"s:{parameter_index}:{window_index}"
            cell_order.append(cell_id)
            evidence_refs.append(evidence_ref)
            source_refs.append(source_ref)
            if supported(parameter_index, window_index):
                supported_scopes.append(
                    SupportedCellScope(
                        cell_id=cell_id,
                        parameter=parameter,
                        window_id=window.window_id,
                        allowed_evidence_refs=(evidence_ref,),
                        allowed_result_sources=(
                            (ResultSource.PREDICTED,)
                            if window.mode is InferenceMode.PREDICTION
                            else (ResultSource.INFERRED,)
                        ),
                        allowed_unit_ids=("mg/l",),
                        allowed_aggregation_methods=(AggregationMethod.MEAN,),
                    )
                )
                continue

            is_blocked = blocked(parameter_index, window_index)
            decision = (
                ReasoningGateDecision.BLOCKED
                if is_blocked
                else ReasoningGateDecision.UNSUPPORTED
            )
            reason = (
                IndeterminateReason.SOURCE_UNAVAILABLE
                if is_blocked
                else IndeterminateReason.INSUFFICIENT_EVIDENCE
            )
            rationale = (
                "Required evidence collection was technically blocked."
                if is_blocked
                else "No eligible evidence supports this cell."
            )
            fixed_cells.append(
                FixedSkeletonCell(
                    decision=decision,
                    result=TimeWindowResult(
                        cell_id=cell_id,
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
                            supporting_refs=(evidence_ref,),
                        ),
                        source=None,
                        confidence=0.0,
                        confidence_rationale=rationale,
                        aggregation_method=None,
                        evidence_refs=(evidence_ref,),
                    ),
                    reasons=(reason.value,),
                    blocker_refs=((evidence_ref,) if is_blocked else ()),
                )
            )
            blocked_count += int(is_blocked)

    modes = {segment.mode for segment in segments}
    mode = next(iter(modes)) if len(modes) == 1 else InferenceMode.MIXED
    values = {
        "schema_version": 1,
        "result_id": result_id,
        "run_id": RUN_ID,
        "session_id": SESSION_ID,
        "target_comid": "12345678",
        "mode": mode,
        "time_range": TimeRange(
            start=segments[0].start,
            end=segments[-1].end,
        ),
        "time_segments": segments,
        "analysis_timezone": "America/Chicago",
        "time_resolution": resolution,
        "time_resolution_source": TimeResolutionSource.FRONTEND,
        "requested_parameters": parameters,
        "unit_preferences": (),
        "windows": tuple(windows),
        "cell_order": tuple(cell_order),
        "fixed_cells": tuple(fixed_cells),
        "supported_scopes": tuple(supported_scopes),
        "evidence_allowlist": tuple(sorted(evidence_refs)),
        "source_record_allowlist": tuple(sorted(source_refs)),
        "context_ref": ItemReference(item_id="context-1"),
        "topology_ref": ItemReference(item_id="topology-1"),
        "bundle_ref": ItemReference(item_id="bundle-1"),
        "bundle_digest": "bundle:sha256:fixed",
        "rule_plan_digest": "rule:sha256:fixed",
        "capability_registry_digest": "sha256:capabilities",
        "planned_cell_count": len(cell_order),
        "fixed_cell_count": len(fixed_cells),
        "supported_cell_count": len(supported_scopes),
        "blocked_cell_count": blocked_count,
        "generated_at": datetime(2026, 10, 2, tzinfo=UTC),
    }
    return ResultSkeleton(
        **values,
        skeleton_digest=canonical_json_digest(values),
    )


def _working_context(
    skeleton: ResultSkeleton,
    *,
    reverse: bool = False,
    include_outside: bool = False,
):
    source_refs = list(skeleton.source_record_allowlist)
    summaries = [
        BoundedEvidenceSummary(
            evidence_ref=evidence_ref,
            summary=f"Bounded quality summary for {evidence_ref}.",
            source_record_item_refs=(source_refs[index],),
            truncated=False,
        )
        for index, evidence_ref in enumerate(skeleton.evidence_allowlist[:64])
    ]
    allowlist = [*skeleton.evidence_allowlist, *skeleton.source_record_allowlist]
    if include_outside:
        allowlist.extend(("e:outside", "s:outside"))
        summaries.append(
            BoundedEvidenceSummary(
                evidence_ref="e:outside",
                summary="Out-of-scope bounded summary.",
                source_record_item_refs=("s:outside",),
                truncated=False,
            )
        )
    if reverse:
        summaries.reverse()
        allowlist.reverse()
    return build_working_context_projection(
        run_id=skeleton.run_id,
        session_id=skeleton.session_id,
        target_comid=skeleton.target_comid,
        scope_digest="sha256:pre-skeleton-scope",
        bundle_ref=skeleton.bundle_ref,
        bundle_digest=skeleton.bundle_digest,
        rule_plan_digest=skeleton.rule_plan_digest,
        capability_registry_digest=skeleton.capability_registry_digest,
        reference_allowlist=tuple(allowlist),
        recent_messages=(),
        facts=(
            WorkingContextFact(
                fact_id="coverage:base",
                kind=WorkingContextFactKind.COVERAGE,
                summary="Base evidence coverage was sealed.",
            ),
        ),
        evidence_summaries=tuple(summaries),
    )


def test_topology_gap_reaches_only_matching_batch_and_full_synthesis() -> None:
    skeleton = _make_skeleton(parameter_count=2)
    base = _working_context(skeleton)
    parameter = skeleton.requested_parameters[0]
    gap = WorkingContextFact(
        fact_id=(
            f"topology_gap:{parameter.code_system}:{parameter.code}:"
            "segment-000:windows:window-000"
        ),
        kind=WorkingContextFactKind.COVERAGE,
        summary="Saved topology topology-1: branch 1776712 has no qualifying source.",
    )
    context = build_working_context_projection(
        run_id=base.run_id,
        session_id=base.session_id,
        target_comid=base.target_comid,
        scope_digest=base.scope_digest,
        bundle_ref=base.bundle_ref,
        bundle_digest=base.bundle_digest,
        rule_plan_digest=base.rule_plan_digest,
        capability_registry_digest=base.capability_registry_digest,
        reference_allowlist=base.reference_allowlist,
        recent_messages=base.recent_messages,
        facts=(*base.facts, gap),
        evidence_summaries=base.evidence_summaries,
        dependency_context=base.dependency_context,
    )

    plan = _plan(skeleton, context=context, planner=_planner(max_cells=1))
    for batch in plan.batch_plan.batches:
        expected = (
            batch.parameter == parameter
            and batch.cells[0].window_id == "window-000"
        )
        assert (gap in batch.context.facts) is expected
        request = PromptBuilder(DEFAULT_REASONING_PROMPT_POLICY).build_reasoning_batch(
            batch.prompt_input()
        )
        assert (gap.summary in request.model_dump_json()) is expected
    assert gap in plan.synthesis_plan.context.facts


def _model_fit(
    *,
    context_tokens: int = 10_000_000,
    output_tokens: int = 1_000_000,
) -> ModelFitCapability:
    return ModelFitCapability(
        model=MODEL,
        context_window_tokens=context_tokens,
        max_output_tokens=output_tokens,
    )


def _planner(
    *,
    max_cells: int = 32,
    max_batches: int = 24,
    max_input_bytes: int = 10_000_000,
    max_output_bytes: int = 10_000_000,
    estimator=None,
) -> ReasoningBatchPlanner:
    if estimator is None:
        estimator = ExactByteEstimator()
    return ReasoningBatchPlanner(
        prompt_builder=PromptBuilder(DEFAULT_REASONING_PROMPT_POLICY),
        model_catalog=CATALOG,
        size_estimator=estimator,
        limits=ReasoningPlannerLimits(
            max_cells_per_batch=max_cells,
            max_batches=max_batches,
            max_serialized_input_bytes=max_input_bytes,
            max_serialized_output_bytes=max_output_bytes,
        ),
    )


def _plan(
    skeleton: ResultSkeleton,
    *,
    planner: ReasoningBatchPlanner | None = None,
    context=None,
    evidence_summaries=None,
    model_fit: ModelFitCapability | None = None,
    reverse_schema: bool = False,
):
    return (planner or _planner()).plan(
        skeleton,
        working_context=context or _working_context(skeleton),
        evidence_summaries=evidence_summaries,
        model_fit=model_fit or _model_fit(),
        deadline=DEADLINE,
        batch_response_schema=_schema("reasoning_batch", reverse=reverse_schema),
        synthesis_response_schema=_schema("overall_synthesis", reverse=reverse_schema),
    )


def _range_for_windows(count: int) -> TimeRange:
    start = datetime(2026, 2, 1)
    return _local_range(start, start + timedelta(hours=6 * count))


def test_maximum_six_parameter_fifteen_day_fixture_is_complete_and_stable() -> None:
    skeleton = _make_skeleton(
        parameter_count=6,
        segment_specs=((_range_for_windows(60), InferenceMode.GAP_FILLING),),
    )

    first = _plan(skeleton)
    second = _plan(skeleton)

    batches = first.batch_plan.batches
    assert canonical_json_text(first.model_dump(mode="json")) == canonical_json_text(
        second.model_dump(mode="json")
    )
    assert first.plan_digest == second.plan_digest
    assert first.batch_plan.supported_cell_count == 360
    assert first.batch_plan.batch_count == 12
    assert [len(batch.cells) for batch in batches] == [32, 28] * 6
    assert [batch.parameter for batch in batches] == [
        parameter
        for parameter in skeleton.requested_parameters
        for _ in range(2)
    ]
    assert tuple(
        cell.cell_id for batch in batches for cell in batch.cells
    ) == tuple(scope.cell_id for scope in skeleton.supported_scopes)
    assert max(batch.serialized_input_bytes for batch in batches) > 0
    assert first.synthesis_plan.reserved_validated_result_bytes > 0


def test_seven_day_batches_include_every_required_evidence_body() -> None:
    original = _make_skeleton(
        segment_specs=((_range_for_windows(28), InferenceMode.GAP_FILLING),),
    )
    scopes = tuple(
        scope.model_copy(update={
            "allowed_evidence_refs": tuple(
                f"e:{index}:{part}" for part in range(3)
            ),
        })
        for index, scope in enumerate(original.supported_scopes)
    )
    references = tuple(
        reference
        for scope in scopes
        for reference in scope.allowed_evidence_refs
    )
    values = original.model_dump(mode="json", exclude={"skeleton_digest"})
    values["supported_scopes"] = tuple(
        scope.model_dump(mode="json") for scope in scopes
    )
    values["evidence_allowlist"] = tuple(sorted(references))
    skeleton = ResultSkeleton(
        **values, skeleton_digest=canonical_json_digest(values)
    )
    summaries = tuple(
        BoundedEvidenceSummary(
            evidence_ref=reference,
            summary=f"measured-content-for-{reference}",
            truncated=False,
        )
        for reference in references
    )
    plan = _plan(
        skeleton,
        context=_working_context(original),
        evidence_summaries=summaries,
    )
    assert len(references) == 84
    assert len(plan.batch_plan.batches) >= 2
    for batch in plan.batch_plan.batches:
        request = PromptBuilder(DEFAULT_REASONING_PROMPT_POLICY).build_reasoning_batch(
            batch.prompt_input()
        )
        serialized = request.model_dump_json()
        for cell in batch.cells:
            for reference in cell.allowed_evidence_refs:
                assert f"measured-content-for-{reference}" in serialized


def test_batch_includes_upstream_content_behind_relation_evidence() -> None:
    original = _make_skeleton()
    first = original.supported_scopes[0]
    relation = first.allowed_evidence_refs[0]
    upstream = "e:upstream:0"
    values = original.model_dump(mode="json", exclude={"skeleton_digest"})
    values["evidence_allowlist"] = tuple(sorted((*original.evidence_allowlist, upstream)))
    skeleton = ResultSkeleton(
        **values, skeleton_digest=canonical_json_digest(values)
    )
    summaries = tuple(
        BoundedEvidenceSummary(
            evidence_ref=reference,
            summary=f"relation-uses-{upstream}" if reference == relation else "local evidence",
            dependency_evidence_refs=(upstream,) if reference == relation else (),
            truncated=False,
        )
        for reference in original.evidence_allowlist
    ) + (BoundedEvidenceSummary(
        evidence_ref=upstream,
        summary="upstream trend rising and risk medium",
        truncated=False,
    ),)
    plan = _plan(
        skeleton,
        context=_working_context(original),
        evidence_summaries=summaries,
    )
    first_batch = plan.batch_plan.batches[0]
    assert upstream not in first_batch.cells[0].allowed_evidence_refs
    request = PromptBuilder(DEFAULT_REASONING_PROMPT_POLICY).build_reasoning_batch(
        first_batch.prompt_input()
    )
    assert "upstream trend rising and risk medium" in request.model_dump_json()


def test_batch_reads_numeric_dependencies_without_extra_result_cells() -> None:
    original = _make_skeleton()
    derived = original.supported_scopes[0].allowed_evidence_refs[0]
    intermediate = "e:intermediate:0"
    input_ref = "e:input:0"
    values = original.model_dump(mode="json", exclude={"skeleton_digest"})
    values["evidence_allowlist"] = tuple(sorted((
        *original.evidence_allowlist, intermediate, input_ref
    )))
    skeleton = ResultSkeleton(
        **values, skeleton_digest=canonical_json_digest(values)
    )
    summaries = tuple(
        BoundedEvidenceSummary(
            evidence_ref=reference,
            summary="target value 8 from registered method"
            if reference == derived else "local evidence",
            dependency_evidence_refs=(intermediate,) if reference == derived else (),
            truncated=False,
        )
        for reference in original.evidence_allowlist
    ) + (
        BoundedEvidenceSummary(
            evidence_ref=intermediate,
            summary="saved intermediate value 6",
            dependency_evidence_refs=(input_ref,),
            truncated=False,
        ),
        BoundedEvidenceSummary(
            evidence_ref=input_ref,
            summary="saved input value 4",
            truncated=False,
        ),
    )
    plan = _plan(
        skeleton,
        context=_working_context(original),
        evidence_summaries=summaries,
    )
    request = PromptBuilder(DEFAULT_REASONING_PROMPT_POLICY).build_reasoning_batch(
        plan.batch_plan.batches[0].prompt_input()
    )
    serialized = request.model_dump_json()

    assert "saved intermediate value 6" in serialized
    assert "saved input value 4" in serialized
    assert intermediate not in plan.batch_plan.supported_cell_order
    assert plan.batch_plan.supported_cell_order == tuple(
        cell.cell_id for cell in original.supported_scopes
    )


def test_real_m5_sealed_handoff_plans_every_supported_cell_once() -> None:
    async def arrange():
        validated, handoff, registry = await _validated_fixture(mode="full")
        sealed = seal_evidence_input(validated, handoff, registry)
        skeleton = build_result_skeleton(
            validated,
            sealed,
            registry,
            result_id="m5-handoff-result",
            generated_at=NOW,
        )
        context = await m5_working_context(validated, handoff, sealed)
        return validated, skeleton, context

    validated, skeleton, context = asyncio.run(arrange())
    selected = validated.task.waterbody_model
    catalog = ModelCatalogSnapshot(
        capabilities=(
            ModelCapability(
                provider=selected.provider,
                model=selected.model,
                roles=(ModelRole.WATERBODY,),
                context_window_tokens=10_000_000,
                max_output_tokens=1_000_000,
            ),
            EMBEDDING_CAPABILITY,
        )
    )
    planner = ReasoningBatchPlanner(
        prompt_builder=PromptBuilder(DEFAULT_REASONING_PROMPT_POLICY),
        model_catalog=catalog,
        size_estimator=ExactByteEstimator(),
        limits=ReasoningPlannerLimits(
            max_serialized_input_bytes=10_000_000,
            max_serialized_output_bytes=10_000_000,
        ),
    )
    plan = planner.plan(
        skeleton,
        working_context=context,
        model_fit=ModelFitCapability(
            model=selected,
            context_window_tokens=10_000_000,
            max_output_tokens=1_000_000,
        ),
        deadline=DEADLINE,
        batch_response_schema=_schema("reasoning_batch"),
        synthesis_response_schema=_schema("overall_synthesis"),
    )

    assert plan.batch_plan.supported_cell_order == tuple(
        scope.cell_id for scope in skeleton.supported_scopes
    )


@pytest.mark.parametrize(
    ("resolution", "expected_windows"),
    (
        (TimeResolution.SIX_HOURS, 4),
        (TimeResolution.TWELVE_HOURS, 2),
        (TimeResolution.TWENTY_FOUR_HOURS, 1),
    ),
)
def test_resolution_and_single_cell_boundaries(
    resolution: TimeResolution,
    expected_windows: int,
) -> None:
    skeleton = _make_skeleton(resolution=resolution)
    plan = _plan(skeleton)

    assert plan.batch_plan.batch_count == 1
    assert len(plan.batch_plan.batches[0].cells) == expected_windows
    assert plan.synthesis_plan.coverage.supported_cells == expected_windows


@pytest.mark.parametrize(
    "start",
    (datetime(2026, 3, 8), datetime(2026, 11, 1)),
)
def test_dst_windows_keep_final_utc_bounds_and_order(start: datetime) -> None:
    requested = _local_range(start, start + timedelta(days=1))
    skeleton = _make_skeleton(segment_specs=((requested, InferenceMode.GAP_FILLING),))
    plan = _plan(skeleton)

    planned = plan.batch_plan.batches[0].cells
    expected = build_time_windows(
        requested,
        TimeResolution.SIX_HOURS,
        "America/Chicago",
    )
    assert [(cell.start_time, cell.end_time) for cell in planned] == [
        (window.start, window.end) for window in expected
    ]


def test_mixed_segments_and_fixed_gaps_are_never_crossed() -> None:
    first = _local_range(datetime(2026, 2, 1), datetime(2026, 2, 2))
    second = _local_range(datetime(2026, 2, 2), datetime(2026, 2, 3))
    mixed = _make_skeleton(
        resolution=TimeResolution.TWELVE_HOURS,
        segment_specs=(
            (first, InferenceMode.GAP_FILLING),
            (second, InferenceMode.PREDICTION),
        ),
    )
    mixed_plan = _plan(mixed)
    assert [
        (batch.segment_index, len(batch.cells))
        for batch in mixed_plan.batch_plan.batches
    ] == [
        (0, 2),
        (1, 2),
    ]

    partial = _make_skeleton(
        supported=lambda _parameter, window: window in {0, 1, 3},
        blocked=lambda _parameter, window: window == 2,
    )
    partial_plan = _plan(partial)
    assert [len(batch.cells) for batch in partial_plan.batch_plan.batches] == [2, 1]
    assert partial_plan.batch_plan.supported_cell_order == (
        partial.cell_order[0],
        partial.cell_order[1],
        partial.cell_order[3],
    )
    assert partial_plan.synthesis_plan.coverage.blocked_cells == 1
    assert partial_plan.synthesis_plan.coverage.unsupported_cells == 0


def test_all_unsupported_has_no_batch_or_synthesis_model_call() -> None:
    skeleton = _make_skeleton(supported=lambda _parameter, _window: False)
    plan = _plan(skeleton)

    assert plan.batch_plan.batches == ()
    assert plan.batch_plan.supported_cell_order == ()
    assert plan.synthesis_plan.requires_model_call is False
    assert plan.synthesis_plan.validated_cell_order == ()
    assert len(plan.synthesis_plan.fixed_cells) == skeleton.planned_cell_count
    assert plan.synthesis_plan.coverage.unsupported_cells == skeleton.planned_cell_count


def test_all_fixed_synthesis_projects_large_sealed_evidence_without_losing_blockers():
    skeleton = _make_skeleton(
        supported=lambda _parameter, _window: False,
        blocked=lambda _parameter, window: window == 0,
        segment_specs=((_range_for_windows(2), InferenceMode.GAP_FILLING),),
    )
    contextual_refs = tuple(
        f"context-{index:04d}-{'c' * 64}" for index in range(860)
    )
    values = skeleton.model_dump(mode="python", exclude={"skeleton_digest"})
    values["evidence_allowlist"] = tuple(sorted((
        *skeleton.evidence_allowlist,
        *contextual_refs,
    )))
    large_skeleton = ResultSkeleton(
        **values,
        skeleton_digest=canonical_json_digest(values),
    )
    summaries = tuple(
        BoundedEvidenceSummary(
            evidence_ref=reference,
            summary="Bounded context. " + "q" * 1580,
            truncated=True,
        )
        for reference in contextual_refs[:64]
    )
    blocker_refs = tuple(
        reference
        for cell in large_skeleton.fixed_cells
        for reference in cell.blocker_refs
    )
    base = build_working_context_projection(
        run_id=large_skeleton.run_id,
        session_id=large_skeleton.session_id,
        target_comid=large_skeleton.target_comid,
        scope_digest="sha256:pre-skeleton-scope",
        bundle_ref=large_skeleton.bundle_ref,
        bundle_digest=large_skeleton.bundle_digest,
        rule_plan_digest=large_skeleton.rule_plan_digest,
        capability_registry_digest=large_skeleton.capability_registry_digest,
        reference_allowlist=tuple(sorted({
            *blocker_refs,
            *(summary.evidence_ref for summary in summaries),
        })),
        recent_messages=(),
        facts=(),
        evidence_summaries=summaries,
    )

    projected = _synthesis_context(
        large_skeleton, base, scope_digest=base.scope_digest,
    )
    assert len(large_skeleton.evidence_allowlist) == 862
    assert projected.reference_allowlist == blocker_refs
    assert projected.evidence_summaries == ()
    assert large_skeleton.fixed_cells[1].result.evidence_refs
    assert large_skeleton.fixed_cells[0].blocker_refs == blocker_refs
    plan = _plan(large_skeleton, context=base)
    assert plan.synthesis_plan.requires_model_call is False
    assert plan.synthesis_plan.context.reference_allowlist == blocker_refs


@pytest.mark.parametrize(
    ("cell_count", "expected_batches"),
    ((32, 1), (33, 2)),
)
def test_cell_count_boundary(cell_count: int, expected_batches: int) -> None:
    skeleton = _make_skeleton(
        segment_specs=((_range_for_windows(cell_count), InferenceMode.GAP_FILLING),),
    )
    plan = _plan(skeleton)
    assert plan.batch_plan.batch_count == expected_batches


@pytest.mark.parametrize("cell_count", (23, 24))
def test_batch_count_boundary_accepts_up_to_twenty_four(cell_count: int) -> None:
    skeleton = _make_skeleton(
        segment_specs=((_range_for_windows(cell_count), InferenceMode.GAP_FILLING),),
    )
    plan = _plan(skeleton, planner=_planner(max_cells=1))
    assert plan.batch_plan.batch_count == cell_count


def test_batch_count_boundary_rejects_twenty_five_before_execution() -> None:
    skeleton = _make_skeleton(
        segment_specs=((_range_for_windows(25), InferenceMode.GAP_FILLING),),
    )
    with pytest.raises(ReasoningPlanTooLargeError) as raised:
        _plan(skeleton, planner=_planner(max_cells=1))
    assert raised.value.code == "reasoning_plan_too_large"


def test_request_output_and_model_fit_limits_are_inclusive_and_fail_closed() -> None:
    skeleton = _make_skeleton(resolution=TimeResolution.TWENTY_FOUR_HOURS)
    baseline = _plan(skeleton)
    batches = baseline.batch_plan.batches
    synthesis = baseline.synthesis_plan
    required_input_bytes = max(
        *(batch.serialized_input_bytes for batch in batches),
        synthesis.serialized_base_input_bytes
        + synthesis.reserved_validated_result_bytes,
    )
    required_output_bytes = max(
        *(batch.estimated_output_bytes for batch in batches),
        synthesis.estimated_output_bytes,
    )
    required_context_tokens = max(
        *(
            batch.estimated_input_tokens + batch.estimated_output_tokens
            for batch in batches
        ),
        synthesis.estimated_input_tokens + synthesis.estimated_output_tokens,
    )
    required_output_tokens = max(
        *(batch.estimated_output_tokens for batch in batches),
        synthesis.estimated_output_tokens,
    )

    _plan(
        skeleton,
        planner=_planner(
            max_input_bytes=required_input_bytes,
            max_output_bytes=required_output_bytes,
        ),
        model_fit=_model_fit(
            context_tokens=required_context_tokens,
            output_tokens=required_output_tokens,
        ),
    )
    failing_cases = (
        (
            _planner(
                max_input_bytes=required_input_bytes - 1,
                max_output_bytes=required_output_bytes,
            ),
            _model_fit(),
        ),
        (
            _planner(
                max_input_bytes=required_input_bytes,
                max_output_bytes=required_output_bytes - 1,
            ),
            _model_fit(),
        ),
        (
            _planner(),
            _model_fit(
                context_tokens=required_context_tokens - 1,
                output_tokens=min(required_output_tokens, required_context_tokens - 1),
            ),
        ),
        (
            _planner(),
            _model_fit(
                context_tokens=required_context_tokens,
                output_tokens=required_output_tokens - 1,
            ),
        ),
    )
    for planner, model_fit in failing_cases:
        with pytest.raises(ReasoningPlanTooLargeError):
            _plan(skeleton, planner=planner, model_fit=model_fit)


def test_canonical_inputs_ignore_mapping_summary_and_completion_order() -> None:
    skeleton = _make_skeleton(parameter_count=2)
    first = _plan(skeleton)
    second = _plan(
        skeleton,
        context=_working_context(skeleton, reverse=True),
        reverse_schema=True,
    )

    assert (
        first.batch_plan.supported_cell_order
        == second.batch_plan.supported_cell_order
    )
    assert tuple(batch.batch_id for batch in first.batch_plan.batches) == tuple(
        batch.batch_id for batch in second.batch_plan.batches
    )
    assert first.plan_digest == second.plan_digest


def test_synthesis_plan_is_bounded_scoped_and_has_no_draft_or_raw_records() -> None:
    skeleton = _make_skeleton(
        supported=lambda _parameter, window: window < 2,
        blocked=lambda _parameter, window: window == 2,
    )
    plan = _plan(
        skeleton,
        context=_working_context(skeleton, include_outside=True),
    )
    synthesis = plan.synthesis_plan
    payload = synthesis.model_dump(mode="json")
    serialized = json.dumps(payload, sort_keys=True)

    assert synthesis.validated_cell_order == skeleton.cell_order[:2]
    assert synthesis.requested_parameters == skeleton.requested_parameters
    assert synthesis.window_ids == tuple(window.window_id for window in skeleton.windows)
    assert tuple(cell.cell_id for cell in synthesis.fixed_cells) == (
        skeleton.cell_order[2:]
    )
    assert "e:outside" not in synthesis.context.reference_allowlist
    assert "Out-of-scope bounded summary" not in serialized
    assert "raw_record" not in serialized
    assert "unvalidated" not in serialized
    assert synthesis.coverage.model_editable is False
    with pytest.raises(ValidationError, match="frozen"):
        synthesis.coverage.fixed_cells = 0

    validated = tuple(
        ValidatedCellPromptSummary(
            cell_id=cell.cell_id,
            window_id=cell.window_id,
            outcome_status=OutcomeStatus.DIRECTIONAL,
            risk_level=RiskLevel.LOW,
            risk_rationale="Eligible evidence supports a low directional risk.",
            confidence=0.7,
            confidence_rationale="The eligible evidence has usable coverage.",
            evidence_refs=cell.allowed_evidence_refs,
        )
        for batch in plan.batch_plan.batches
        for cell in batch.cells
    )
    prompt = synthesis.prompt_input(validated)
    assert prompt.requested_parameters == skeleton.requested_parameters
    assert prompt.window_ids == tuple(window.window_id for window in skeleton.windows)
    assert tuple(cell.cell_id for cell in prompt.validated_cells) == (
        synthesis.validated_cell_order
    )
    with pytest.raises(
        ReasoningPlanError,
        match="reasoning_synthesis_cells_incomplete",
    ):
        synthesis.prompt_input(tuple(reversed(validated)))
    with pytest.raises(
        ReasoningPlanError,
        match="reasoning_synthesis_evidence_out_of_scope",
    ):
        synthesis.prompt_input(
            (
                validated[0].model_copy(update={"evidence_refs": ("e:1:0",)}),
                validated[1],
            )
        )


def test_model_catalog_authority_and_estimator_receive_canonical_bytes() -> None:
    skeleton = _make_skeleton()

    class RecordingEstimator:
        def __init__(self) -> None:
            self.requests: list[bytes] = []
            self.shapes: list[bytes] = []

        def estimate(self, model, *, serialized_request, serialized_response_shape):
            self.requests.append(serialized_request)
            self.shapes.append(serialized_response_shape)
            return ExactByteEstimator().estimate(
                model,
                serialized_request=serialized_request,
                serialized_response_shape=serialized_response_shape,
            )

    estimator = RecordingEstimator()
    _plan(skeleton, planner=_planner(estimator=estimator))
    assert estimator.requests
    assert all(json.loads(payload) for payload in estimator.requests)
    assert all(json.loads(payload)["response_schema"] for payload in estimator.shapes)

    unauthorized = ModelFitCapability(
        model=ModelSelection(provider="other", model="model"),
        context_window_tokens=10_000,
        max_output_tokens=1_000,
    )
    with pytest.raises(ReasoningPlanError, match="reasoning_model_not_authorized"):
        _plan(skeleton, model_fit=unauthorized)


def test_hash_seed_audit_has_a_fixed_plan_digest() -> None:
    skeleton = _make_skeleton(parameter_count=2)
    plan = _plan(skeleton)
    assert plan.plan_digest == (
        "sha256:87e42480f63bd68918119a4ffc7d8548b23771503c0346afcb6dddecd8f63dc0"
    )
