import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.application.reasoning import (
    FixedSkeletonCell,
    ModelFitCapability,
    ModelSizeEstimate,
    ReasoningBatchPlanner,
    ReasoningGateDecision,
    ReasoningPlanError,
    ReasoningPlanTooLargeError,
    ReasoningPlannerLimits,
    ResultSkeleton,
    ResultSkeletonWindow,
    SupportedCellScope,
    build_result_skeleton,
    seal_evidence_input,
)
from app.application.reasoning.model_models import WaterbodyJsonSchema
from app.application.reasoning.prompt_builder import (
    PromptBuilder,
    ValidatedCellPromptSummary,
)
from app.application.reasoning.prompt_policy import (
    DEFAULT_REASONING_PROMPT_POLICY,
    canonical_json_digest,
    canonical_json_text,
)
from app.application.reasoning.planning_loop import (
    _working_context as m5_working_context,
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
from tests.application.reasoning.test_task_and_skeleton import (
    NOW,
    _validated_fixture,
)


RUN_ID = UUID("10000000-0000-4000-8000-000000000001")
SESSION_ID = UUID("20000000-0000-4000-8000-000000000001")
MODEL = ModelSelection(provider="test-provider", model="waterbody-model")
DEADLINE = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
CATALOG = ModelCatalogSnapshot(
    capabilities=(
        ModelCapability(
            provider=MODEL.provider,
            model=MODEL.model,
            roles=(ModelRole.WATERBODY,),
            context_window_tokens=10_000_000,
            max_output_tokens=1_000_000,
        ),
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
        "generated_at": datetime(2026, 10, 2, tzinfo=timezone.utc),
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
    estimator=ExactByteEstimator(),
) -> ReasoningBatchPlanner:
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
    model_fit: ModelFitCapability | None = None,
    reverse_schema: bool = False,
):
    return (planner or _planner()).plan(
        skeleton,
        working_context=context or _working_context(skeleton),
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
        "sha256:99edb3f6eb9631f966795eeb9cf76524487f1d1f43342c547d86c4b0ccc9ce59"
    )
