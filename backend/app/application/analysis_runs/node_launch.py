from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import StrictBool, field_validator, model_validator

from app.application.analysis_runs.topology_result_models import (
    DependencyStatus,
    WaterbodyDependencyManifest,
)
from app.application.evidence_tools import EvidenceAcquisitionHandoff
from app.application.reasoning.task import ValidatedWaterbodyAgentTask
from app.domain import (
    DomainEnum,
    DomainModel,
    EvidenceCellRole,
    NonEmptyString,
    ParameterKey,
    ensure_unique_tuple,
)


_DISCHARGE = ParameterKey(code_system="usgs", code="00060")


class NodeLaunchAction(DomainEnum):
    RUN_AGENT = "run_agent"
    DETERMINISTIC_INSUFFICIENT = "deterministic_insufficient"
    INVALID = "invalid"
    CANCELLED = "cancelled"


class NodeLaunchFacts(DomainModel):
    run_id: UUID
    session_id: UUID
    target_comid: NonEmptyString
    topology_item_id: UUID
    station_backed: StrictBool
    direct_upstream_comids: tuple[NonEmptyString, ...]
    collected_usable_upstream_comids: tuple[NonEmptyString, ...]
    local_target_usable: StrictBool
    local_discharge_usable: StrictBool
    preflight_blocked: StrictBool
    preflight_cancelled: StrictBool
    stop_before_model: StrictBool

    @field_validator(
        "direct_upstream_comids",
        "collected_usable_upstream_comids",
    )
    @classmethod
    def canonicalize_upstreams(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        ensure_unique_tuple(value, field_name="direct upstream COMIDs")
        return tuple(sorted(value))


class NodeLaunchDecision(DomainModel):
    action: NodeLaunchAction
    reason_codes: tuple[NonEmptyString, ...]
    usable_upstream_comids: tuple[NonEmptyString, ...] = ()

    @field_validator("reason_codes")
    @classmethod
    def validate_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("node launch decision requires an auditable reason")
        return ensure_unique_tuple(value, field_name="node launch reason codes")

    @field_validator("usable_upstream_comids")
    @classmethod
    def canonicalize_usable_upstreams(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        ensure_unique_tuple(value, field_name="usable upstream COMIDs")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def validate_projection(self) -> Self:
        if self.action is not NodeLaunchAction.RUN_AGENT and (
            self.usable_upstream_comids
        ):
            raise ValueError("only a runnable node may expose usable upstream COMIDs")
        return self


class NodeLaunchPolicy:
    """Choose a node path from server-owned topology and acquisition facts."""

    def decide(
        self,
        facts: NodeLaunchFacts,
        manifest: WaterbodyDependencyManifest,
    ) -> NodeLaunchDecision:
        if facts.preflight_cancelled:
            return _decision(NodeLaunchAction.CANCELLED, "node_cancelled")

        if not _manifest_matches(facts, manifest):
            return _decision(NodeLaunchAction.INVALID, "dependency_manifest_invalid")

        if any(
            dependency.dependency_status is DependencyStatus.CANCELLED
            for dependency in manifest.dependencies
        ):
            return _decision(NodeLaunchAction.CANCELLED, "dependency_cancelled")

        usable_upstreams = tuple(
            dependency.upstream_comid
            for dependency in manifest.dependencies
            if dependency.dependency_status is DependencyStatus.USABLE
        )
        if facts.collected_usable_upstream_comids != usable_upstreams:
            return _decision(NodeLaunchAction.INVALID, "upstream_collection_mismatch")
        if facts.station_backed:
            runnable = facts.local_target_usable or (
                facts.local_discharge_usable and bool(usable_upstreams)
            )
            reason = (
                "local_target_and_discharge_usable"
                if facts.local_target_usable and facts.local_discharge_usable
                else "local_target_usable"
                if facts.local_target_usable
                else "local_discharge_and_upstream_usable"
            )
        else:
            runnable = bool(usable_upstreams)
            reason = "stationless_upstream_usable"

        expected_stop = not runnable and not facts.preflight_blocked
        if facts.stop_before_model is not expected_stop:
            return _decision(NodeLaunchAction.INVALID, "preflight_launch_gate_mismatch")
        if runnable:
            return NodeLaunchDecision(
                action=NodeLaunchAction.RUN_AGENT,
                reason_codes=(reason,),
                usable_upstream_comids=usable_upstreams,
            )
        if facts.preflight_blocked:
            return _decision(NodeLaunchAction.INVALID, "evidence_preflight_failed")
        return _decision(
            NodeLaunchAction.DETERMINISTIC_INSUFFICIENT,
            (
                "no_usable_local_or_upstream_evidence"
                if facts.station_backed
                else "upstream_dependencies_insufficient"
            ),
        )


def build_node_launch_facts(
    task: ValidatedWaterbodyAgentTask,
    handoff: EvidenceAcquisitionHandoff,
) -> NodeLaunchFacts:
    scope = task.task.evidence_scope
    if (
        handoff.run_id != str(task.task.run_id)
        or handoff.session_id != str(task.task.session_id)
        or not handoff.has_valid_bundle_binding()
    ):
        raise ValueError("evidence handoff does not match the waterbody task")
    target_cells = tuple(
        cell for cell in handoff.cells if cell.role is EvidenceCellRole.TARGET
    )
    discharge_cells = tuple(
        cell
        for cell in handoff.cells
        if cell.parameter == _DISCHARGE
    )
    return NodeLaunchFacts(
        run_id=task.task.run_id,
        session_id=task.task.session_id,
        target_comid=task.task.target_comid,
        topology_item_id=UUID(task.task.topology_ref.item_id),
        station_backed=bool(scope.station_refs),
        direct_upstream_comids=scope.direct_upstream_comids,
        collected_usable_upstream_comids=handoff.usable_upstream_comids,
        local_target_usable=any(cell.direct_fragment_refs for cell in target_cells),
        local_discharge_usable=any(
            cell.direct_fragment_refs for cell in discharge_cells
        ),
        preflight_blocked=bool(handoff.block_facts),
        preflight_cancelled=task.task.cancellation_token.is_requested(),
        stop_before_model=handoff.stop_before_model,
    )


def _manifest_matches(
    facts: NodeLaunchFacts,
    manifest: WaterbodyDependencyManifest,
) -> bool:
    return (
        manifest.run_id == facts.run_id
        and manifest.session_id == facts.session_id
        and manifest.target_comid == facts.target_comid
        and manifest.topology_ref.item_id == facts.topology_item_id
        and tuple(
            dependency.upstream_comid for dependency in manifest.dependencies
        )
        == facts.direct_upstream_comids
    )


def _decision(action: NodeLaunchAction, reason: str) -> NodeLaunchDecision:
    return NodeLaunchDecision(action=action, reason_codes=(reason,))
