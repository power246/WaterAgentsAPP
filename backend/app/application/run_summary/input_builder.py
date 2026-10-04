from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.application.analysis_runs.models import SessionResultReference
from app.application.analysis_runs.topology_result_assembler import (
    WaterbodyOutcomeAggregation,
)
from app.application.analysis_runs.topology_result_models import (
    DependencyOutcome,
    DependencyStatus,
    GraphCoverage,
    StableSourceReference,
)
from app.application.canonical_json import canonical_json_digest
from app.application.evidence_tools import UpstreamInferenceResultReader
from app.application.evidence_tools.upstream_projection import (
    ResolvedInferenceResult,
    validate_resolved_outcome,
)
from app.application.reasoning.batch_planner import (
    ModelFitCapability,
    ModelSizeEstimate,
)
from app.application.run_summary.draft_models import run_summary_response_schema
from app.application.run_summary.errors import SummaryInputTooLargeError
from app.application.run_summary.models import (
    NotableFinding,
    ReportWindowSummary,
    RunSummaryInput,
    RunSummaryLimitFact,
    TargetResultProjection,
    WaterbodyReportCard,
)
from app.application.run_summary.prompt_builder import (
    SUMMARY_SYSTEM_PROMPT,
    summary_prompt_text,
)
from app.application.transcripts.models import TranscriptContentRef
from app.domain import (
    HydrologyGraph,
    HydrologyParticipation,
    InferenceResult,
    ModelSelection,
    OutcomeStatus,
)


class RunSummarySizeEstimator(Protocol):
    def estimate(
        self,
        model: ModelSelection,
        *,
        serialized_request: bytes,
        serialized_response_shape: bytes,
    ) -> ModelSizeEstimate: ...


@dataclass(frozen=True, slots=True)
class RunSummaryInputLimits:
    max_serialized_input_bytes: int = 262_144
    max_highlights: int = 32

    def __post_init__(self) -> None:
        if self.max_serialized_input_bytes <= 0 or self.max_highlights <= 0:
            raise ValueError("run summary input limits must be positive")


class RunSummaryInputBuilder:
    """Build one complete, stable summary input from committed result facts."""

    def __init__(
        self,
        *,
        result_reader: UpstreamInferenceResultReader,
        size_estimator: RunSummarySizeEstimator,
        limits: RunSummaryInputLimits | None = None,
    ) -> None:
        self._result_reader = result_reader
        self._size_estimator = size_estimator
        self._limits = limits or RunSummaryInputLimits()

    async def build(
        self,
        *,
        run_id: UUID,
        session_id: UUID,
        query: str,
        topology: HydrologyGraph,
        topology_ref: TranscriptContentRef,
        aggregation: WaterbodyOutcomeAggregation,
        model: ModelSelection,
        model_fit: ModelFitCapability,
    ) -> RunSummaryInput:
        if topology_ref.run_id != run_id:
            raise ValueError("summary topology reference must remain in the run")
        if model_fit.model != model:
            raise ValueError("summary model-fit identity must match the run model")

        order = tuple(
            comid for layer in topology.execution_layers for comid in layer.root
        )
        active = {
            node.comid
            for node in topology.nodes
            if node.participation is HydrologyParticipation.ACTIVE
        }
        by_comid = {outcome.target_comid: outcome for outcome in aggregation.outcomes}
        if (
            len(by_comid) != len(aggregation.outcomes)
            or set(by_comid) != active
            or set(order) != active
            or order[-1] != topology.target_comid
        ):
            raise ValueError("summary outcomes must exactly cover active topology")

        resolved: dict[str, ResolvedInferenceResult] = {}
        for comid in order:
            outcome = by_comid[comid]
            if outcome.result_ref is None:
                continue
            value = await self._result_reader.resolve(outcome)
            validate_resolved_outcome(outcome, value)
            resolved[comid] = value

        target_outcome = by_comid[topology.target_comid]
        if aggregation.target_outcome != target_outcome:
            raise ValueError("summary target outcome does not match active topology")
        target_resolved = resolved.get(topology.target_comid)
        if target_resolved is None:
            raise ValueError("target outcome must contain a committed result")

        topology_source = StableSourceReference(
            run_id=run_id,
            item_id=topology_ref.item_id,
            content_type="topology_ready",
            content_schema_version=1,
        )
        direct_upstream = _direct_upstream(topology)
        cards = tuple(
            _build_card(
                outcome=by_comid[comid],
                graph_position=index,
                direct_upstream_comids=direct_upstream[comid],
                resolved=resolved.get(comid),
            )
            for index, comid in enumerate(order[:-1])
        )
        target = _build_target(
            outcome=target_outcome,
            graph_position=len(order) - 1,
            direct_upstream_comids=direct_upstream[topology.target_comid],
            resolved=target_resolved,
        )
        limits = _limit_facts(
            aggregation.graph_coverage,
            tuple(by_comid[comid] for comid in order),
            topology_source,
        )
        allowed_refs = tuple(
            sorted(
                {
                *target.source_refs,
                *(reference for card in cards for reference in card.source_refs),
                *(fact.source_ref for fact in limits),
                },
                key=lambda reference: (
                    str(reference.run_id),
                    str(reference.session_id or ""),
                    str(reference.item_id),
                    reference.content_type,
                    reference.result_id or "",
                    reference.cell_id or "",
                    reference.window_id or "",
                ),
            )
        )
        values = {
            "schema_version": 1,
            "run_id": run_id,
            "session_id": session_id,
            "query": query,
            "target_comid": topology.target_comid,
            "mode": target_resolved.result.mode,
            "time_range": target_resolved.result.time_range,
            "analysis_timezone": target_resolved.result.analysis_timezone,
            "graph_coverage": aggregation.graph_coverage,
            "limit_facts": limits,
            "target_result_projection": target,
            "report_cards": cards,
            "allowed_source_refs": allowed_refs,
        }
        summary_input = RunSummaryInput(
            **values,
            input_digest=canonical_json_digest(values),
        )
        self._require_fit(summary_input, model=model, model_fit=model_fit)
        return summary_input

    def _require_fit(
        self,
        summary_input: RunSummaryInput,
        *,
        model: ModelSelection,
        model_fit: ModelFitCapability,
    ) -> None:
        serialized = json.dumps(
            {
                "system_prompt": SUMMARY_SYSTEM_PROMPT,
                "messages": (
                    {
                        "role": "user",
                        "content": summary_prompt_text(summary_input),
                    },
                ),
                "response_schema": run_summary_response_schema().model_dump(
                    mode="json"
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(serialized) > self._limits.max_serialized_input_bytes:
            raise SummaryInputTooLargeError()
        response_shape = json.dumps(
            {
                "summary_highlight_limit": self._limits.max_highlights,
                "schema": json.loads(run_summary_response_schema().schema_text),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        estimate = self._size_estimator.estimate(
            model,
            serialized_request=serialized,
            serialized_response_shape=response_shape,
        )
        if (
            estimate.output_tokens > model_fit.max_output_tokens
            or estimate.input_tokens + model_fit.max_output_tokens
            > model_fit.context_window_tokens
        ):
            raise SummaryInputTooLargeError()


def _build_target(
    *,
    outcome: DependencyOutcome,
    graph_position: int,
    direct_upstream_comids: tuple[str, ...],
    resolved: ResolvedInferenceResult,
) -> TargetResultProjection:
    result = resolved.result
    if outcome.result_ref is None:
        raise ValueError("target projection requires a result reference")
    common = _result_projection(
        outcome=outcome,
        result=result,
        graph_position=graph_position,
        direct_upstream_comids=direct_upstream_comids,
    )
    return TargetResultProjection(
        **common,
        time_range=result.time_range,
        analysis_timezone=result.analysis_timezone,
        full_result=result,
    )


def _build_card(
    *,
    outcome: DependencyOutcome,
    graph_position: int,
    direct_upstream_comids: tuple[str, ...],
    resolved: ResolvedInferenceResult | None,
) -> WaterbodyReportCard:
    base = {
        "target_comid": outcome.target_comid,
        "session_id": outcome.session_id,
        "graph_position": graph_position,
        "direct_upstream_comids": direct_upstream_comids,
        "dependency_status": outcome.status,
    }
    if resolved is None:
        return WaterbodyReportCard(**base)
    return WaterbodyReportCard(
        **base,
        **_result_projection(
            outcome=outcome,
            result=resolved.result,
            graph_position=graph_position,
            direct_upstream_comids=direct_upstream_comids,
            include_identity=False,
        ),
    )


def _result_projection(
    *,
    outcome: DependencyOutcome,
    result: InferenceResult,
    graph_position: int,
    direct_upstream_comids: tuple[str, ...],
    include_identity: bool = True,
) -> dict[str, object]:
    if outcome.result_ref is None:
        raise ValueError("result projection requires a result reference")
    refs_by_cell = {
        reference.cell_id: reference for reference in outcome.usable_cell_refs
    }
    window_summaries: list[ReportWindowSummary] = []
    for index, assessment in enumerate(result.window_assessments):
        points = tuple(series.points[index] for series in result.series)
        refs = tuple(
            refs_by_cell[point.cell_id]
            for point in points
            if point.cell_id in refs_by_cell
        )
        window_id = (
            refs[0].window_id
            if refs
            else _window_id(points[0].cell_id)
        )
        window_summaries.append(
            ReportWindowSummary(
                window_id=window_id,
                overall_risk=assessment.risk_level,
                availability=assessment.availability,
                confidence=assessment.confidence,
                key_reasons=_bounded_reasons(
                    assessment.risk_rationale,
                    assessment.confidence_rationale,
                ),
                source_refs=refs,
            )
        )
    notable: list[NotableFinding] = []
    for series in result.series:
        for point in series.points:
            reference = refs_by_cell.get(point.cell_id)
            if reference is None or len(notable) == 16:
                continue
            notable.append(
                NotableFinding(
                    kind=(
                        "notable_value"
                        if point.outcome_status is OutcomeStatus.QUANTIFIED
                        else "directional_trend"
                    ),
                    text=point.assessment.risk_rationale,
                    source_ref=reference,
                    display_value=point.representative_value,
                    display_unit=point.unit_id,
                    window_id=(
                        reference.window_id
                        if point.representative_value is not None
                        else None
                    ),
                )
            )
    projection: dict[str, object] = {
        "result_ref": SessionResultReference(
            run_id=outcome.run_id,
            item_id=outcome.result_ref.item_id,
        ),
        "mode": result.mode,
        "overall_risk": result.overall_assessment.risk_level,
        "availability": result.overall_assessment.availability,
        "confidence": result.overall_confidence,
        "key_reasons": _bounded_reasons(
            result.overall_assessment.risk_rationale,
            result.overall_confidence_rationale,
        ),
        "window_summaries": tuple(window_summaries),
        "notable_findings": tuple(notable),
        "model_execution_summary": result.model_execution_summary,
    }
    if include_identity:
        projection.update(
            target_comid=outcome.target_comid,
            session_id=outcome.session_id,
            graph_position=graph_position,
            direct_upstream_comids=direct_upstream_comids,
        )
    return projection


def _direct_upstream(topology: HydrologyGraph) -> dict[str, tuple[str, ...]]:
    active = {
        node.comid
        for node in topology.nodes
        if node.participation is HydrologyParticipation.ACTIVE
    }
    return {
        comid: tuple(
            edge.upstream_comid
            for edge in topology.edges
            if edge.participating
            and edge.downstream_comid == comid
            and edge.upstream_comid in active
        )
        for comid in active
    }


def _limit_facts(
    coverage: GraphCoverage,
    outcomes: tuple[DependencyOutcome, ...],
    topology_source: StableSourceReference,
) -> tuple[RunSummaryLimitFact, ...]:
    facts: list[RunSummaryLimitFact] = []
    if coverage is not GraphCoverage.COMPLETE:
        facts.append(
            RunSummaryLimitFact(
                code="partial_topology_coverage",
                text="The analysis does not have complete upstream branch coverage.",
                source_ref=topology_source,
            )
        )
    invalid = tuple(
        outcome.target_comid
        for outcome in outcomes
        if outcome.status in {DependencyStatus.INVALID, DependencyStatus.CANCELLED}
    )
    if invalid:
        facts.append(
            RunSummaryLimitFact(
                code="invalid_upstream_branches",
                text=(
                    "Some upstream branches did not provide a valid result: "
                    + ", ".join(invalid)
                    + "."
                ),
                source_ref=topology_source,
            )
        )
    return tuple(facts)


def _bounded_reasons(*values: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))[:8]


def _window_id(cell_id: str) -> str:
    parts = cell_id.split(":", 3)
    if len(parts) != 4 or not parts[3]:
        raise ValueError("result cell ID does not preserve window identity")
    return parts[3]
