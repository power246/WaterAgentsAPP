from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.application.analysis_runs.models import (
    AgentRole,
    AgentSessionStatus,
    SessionSummary,
    WaterbodySessionResultSummary,
)
from app.application.analysis_runs.topology_result_models import (
    DependencyDischargeState,
    DependencyOutcome,
    DependencyStatus,
    GraphCoverage,
    StableSourceReference,
)
from app.application.evidence_tools import (
    UpstreamInferenceResultReader,
)
from app.application.evidence_tools.upstream_projection import (
    validate_resolved_outcome,
)
from app.domain import (
    HydrologyGraph,
    HydrologyParticipation,
    OutcomeStatus,
    ParameterKey,
    TopologyCoverage,
)


_DISCHARGE = ParameterKey(code_system="usgs", code="00060")


class TargetWaterbodyFailedError(RuntimeError):
    pass


class TargetWaterbodyCancelledError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WaterbodyOutcomeAggregation:
    target_outcome: DependencyOutcome
    outcomes: tuple[DependencyOutcome, ...]
    graph_coverage: GraphCoverage


class TopologyResultAssembler:
    """Build scheduler outcomes only from committed coordinator state."""

    def __init__(self, result_reader: UpstreamInferenceResultReader) -> None:
        self._result_reader = result_reader

    async def dependency_outcome(
        self,
        run_id: UUID,
        session: SessionSummary,
    ) -> DependencyOutcome:
        if session.agent_role is not AgentRole.WATERBODY or session.target_comid is None:
            raise ValueError("dependency outcome requires a waterbody session")
        base = {
            "run_id": run_id,
            "target_comid": session.target_comid,
            "session_id": session.session_id,
            "completed_at": session.updated_at,
        }
        if session.status is AgentSessionStatus.FAILED:
            return DependencyOutcome(**base, status=DependencyStatus.INVALID)
        if session.status is AgentSessionStatus.CANCELLED:
            return DependencyOutcome(**base, status=DependencyStatus.CANCELLED)
        if session.status is not AgentSessionStatus.COMPLETED:
            raise ValueError("dependency outcome requires a terminal session")
        if (
            session.result_ref is None
            or not isinstance(
                session.result_summary,
                WaterbodySessionResultSummary,
            )
        ):
            raise ValueError("completed waterbody session lacks its committed result")

        unresolved = DependencyOutcome(
            **base,
            status=DependencyStatus.INSUFFICIENT,
            result_ref=session.result_ref,
            result_summary=session.result_summary,
        )
        resolved = await self._result_reader.resolve(unresolved)
        result = resolved.result
        eligible = tuple(
            point
            for series in result.series
            for point in series.points
            if point.outcome_status
            in {OutcomeStatus.QUANTIFIED, OutcomeStatus.DIRECTIONAL}
        )
        references = tuple(
            StableSourceReference(
                run_id=run_id,
                session_id=session.session_id,
                item_id=session.result_ref.item_id,
                content_type="inference_result",
                content_schema_version=1,
                result_id=result.result_id,
                cell_id=point.cell_id,
                window_id=_window_id(point.cell_id),
            )
            for point in eligible
        )
        discharge_states = tuple(
            DependencyDischargeState(
                window_id=_window_id(point.cell_id),
                outcome=point.outcome_status,
            )
            for series in result.series
            if series.parameter.key == _DISCHARGE
            for point in series.points
        )
        outcome = DependencyOutcome(
            **base,
            status=(
                DependencyStatus.USABLE
                if references
                else DependencyStatus.INSUFFICIENT
            ),
            result_ref=session.result_ref,
            result_summary=session.result_summary,
            usable_cell_refs=references,
            discharge_states=discharge_states if references else (),
        )
        validate_resolved_outcome(outcome, resolved)
        return outcome

    def aggregate(
        self,
        topology: HydrologyGraph,
        outcomes: tuple[DependencyOutcome, ...],
    ) -> WaterbodyOutcomeAggregation:
        order = tuple(
            comid for layer in topology.execution_layers for comid in layer.root
        )
        by_comid = {outcome.target_comid: outcome for outcome in outcomes}
        active = {
            node.comid
            for node in topology.nodes
            if node.participation is HydrologyParticipation.ACTIVE
        }
        if len(by_comid) != len(outcomes) or set(by_comid) != active:
            raise ValueError("waterbody outcomes must exactly cover active topology")
        ordered = tuple(by_comid[comid] for comid in order)
        target = by_comid[topology.target_comid]
        if target.status is DependencyStatus.INVALID:
            raise TargetWaterbodyFailedError("target waterbody failed")
        if target.status is DependencyStatus.CANCELLED:
            raise TargetWaterbodyCancelledError("target waterbody was cancelled")
        if topology.coverage is TopologyCoverage.NONE:
            coverage = GraphCoverage.NONE
        elif (
            topology.coverage is TopologyCoverage.PARTIAL
            or any(
                outcome.status
                in {DependencyStatus.INVALID, DependencyStatus.CANCELLED}
                for outcome in ordered
            )
        ):
            coverage = GraphCoverage.PARTIAL
        else:
            coverage = GraphCoverage.COMPLETE
        return WaterbodyOutcomeAggregation(
            target_outcome=target,
            outcomes=ordered,
            graph_coverage=coverage,
        )


def _window_id(cell_id: str) -> str:
    parts = cell_id.split(":", 3)
    if len(parts) != 4 or parts[0] != "cell" or not parts[3]:
        raise ValueError("result cell ID does not preserve window identity")
    return parts[3]
