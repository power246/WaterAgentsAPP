from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

from app.application.analysis_runs.models import (
    AgentRole,
    AgentSessionStatus,
    AnalysisRunSnapshot,
    RunStatus,
    SessionSummary,
)
from app.application.analysis_runs.topology_result_models import (
    WaterbodyDependencyManifest,
)
from app.application.analysis_runs.workflow import CancellationToken
from app.application.evidence_tools import (
    EvidenceScopeViolationError,
    EvidenceSessionScope,
    EvidenceToolReader,
)
from app.application.query_understanding import AnalysisContext
from app.application.reasoning.errors import WaterbodyTaskValidationError
from app.domain import (
    HydrologyGraph,
    ItemReference,
    MemoryMode,
    ModelCatalogSnapshot,
    ModelRole,
    ModelSelection,
    build_time_windows,
)

if TYPE_CHECKING:
    from app.application.guidelines.retrieval import GuidelineSessionKnowledge


@dataclass(frozen=True, slots=True)
class WaterbodyAgentTask:
    run_id: UUID
    session_id: UUID
    target_comid: str
    analysis_context_ref: ItemReference
    topology_ref: ItemReference
    evidence_scope: EvidenceSessionScope
    waterbody_model: ModelSelection
    memory_mode: MemoryMode
    cancellation_token: CancellationToken
    dependency_manifest: WaterbodyDependencyManifest | None = None
    agent_role: AgentRole = AgentRole.WATERBODY
    guidelines: "GuidelineSessionKnowledge | None" = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.target_comid.strip():
            raise ValueError("target_comid must not be empty")
        if self.agent_role is not AgentRole.WATERBODY:
            raise ValueError("WaterbodyAgentTask requires the waterbody role")


@dataclass(frozen=True, slots=True)
class ValidatedWaterbodyAgentTask:
    task: WaterbodyAgentTask
    run: AnalysisRunSnapshot
    session: SessionSummary
    context: AnalysisContext
    topology: HydrologyGraph


async def validate_waterbody_agent_task(
    task: WaterbodyAgentTask,
    *,
    run: AnalysisRunSnapshot,
    context: AnalysisContext,
    topology: HydrologyGraph,
    model_catalog: ModelCatalogSnapshot,
    session_reader: EvidenceToolReader,
) -> ValidatedWaterbodyAgentTask:
    scope = task.evidence_scope
    _require(
        task.cancellation_token is scope.cancel_token,
        "task_cancellation_scope_mismatch",
    )
    if task.cancellation_token.is_requested():
        raise WaterbodyTaskValidationError("task_cancelled_before_start")

    _require(run.run_id == task.run_id, "task_run_mismatch")
    _require(run.status is RunStatus.RUNNING, "task_run_not_active")
    _require(str(task.run_id) == scope.run_id, "task_scope_run_mismatch")
    _require(str(task.session_id) == scope.session_id, "task_scope_session_mismatch")
    _require(
        scope.session_ref.run_id == scope.run_id
        and scope.session_ref.session_id == scope.session_id,
        "task_session_scope_mismatch",
    )
    _require(task.target_comid == scope.agent_comid, "task_comid_mismatch")
    _require(
        task.analysis_context_ref.item_id == scope.analysis_context_ref.item_id
        and scope.analysis_context_ref.run_id == scope.run_id
        and scope.analysis_context_ref.session_id is None,
        "task_context_ref_mismatch",
    )
    _require(
        task.topology_ref.item_id == scope.topology_ref.item_id
        and scope.topology_ref.run_id == scope.run_id
        and scope.topology_ref.session_id is None,
        "task_topology_ref_mismatch",
    )
    _require(
        task.waterbody_model == context.waterbody_model
        and task.waterbody_model == run.configuration.waterbody_model,
        "task_model_selection_mismatch",
    )
    _require(
        model_catalog.supports(task.waterbody_model, ModelRole.WATERBODY),
        "task_model_role_unsupported",
    )
    _require(
        task.memory_mode is context.memory_mode
        and task.memory_mode is run.configuration.memory_mode
        and task.memory_mode is scope.memory_mode,
        "task_memory_mode_mismatch",
    )
    _require(
        run.configuration.analysis_timezone == context.analysis_timezone,
        "task_run_context_timezone_mismatch",
    )
    _require(
        not context.unit_preferences or (
            run.configuration.unit_registry_digest != "unknown"
            and all(
                item.registry_digest == run.configuration.unit_registry_digest
                for item in context.unit_preferences
            )
        ),
        "task_unit_registry_mismatch",
    )
    _validate_scope_facts(scope, context=context, topology=topology)
    if task.dependency_manifest is not None:
        _validate_dependency_manifest(task, task.dependency_manifest)

    try:
        session = await session_reader.read_agent_session(scope, scope.session_id)
    except EvidenceScopeViolationError as error:
        raise WaterbodyTaskValidationError("task_session_not_found") from error
    _require(session.session_id == task.session_id, "task_session_identity_mismatch")
    _require(session.agent_role is AgentRole.WATERBODY, "task_session_role_mismatch")
    _require(session.target_comid == task.target_comid, "task_session_comid_mismatch")
    _require(
        session.status is AgentSessionStatus.PENDING,
        "task_session_not_startable",
    )
    if task.cancellation_token.is_requested():
        raise WaterbodyTaskValidationError("task_cancelled_before_start")
    return ValidatedWaterbodyAgentTask(
        task=task,
        run=run,
        session=session,
        context=context,
        topology=topology,
    )


def _validate_scope_facts(
    scope: EvidenceSessionScope,
    *,
    context: AnalysisContext,
    topology: HydrologyGraph,
) -> None:
    _require(scope.topology == topology, "task_scope_topology_mismatch")
    _require(context.target.comid == topology.target_comid, "task_context_topology_mismatch")
    node = next(
        (candidate for candidate in topology.nodes if candidate.comid == scope.agent_comid),
        None,
    )
    _require(node is not None and node == scope.target_node, "task_scope_node_mismatch")
    _require(scope.query == context.query, "task_scope_query_mismatch")
    _require(
        scope.analysis_target_point == context.target.point,
        "task_scope_target_point_mismatch",
    )
    _require(
        scope.requested_parameters == tuple(item.key for item in context.params_used),
        "task_scope_requested_parameters_mismatch",
    )
    _require(
        scope.supporting_parameters
        == tuple(item.key for item in context.supporting_params),
        "task_scope_supporting_parameters_mismatch",
    )
    _require(
        scope.evidence_rule_plan == context.evidence_rule_plan,
        "task_scope_rule_plan_mismatch",
    )
    _require(scope.mode is context.mode, "task_scope_mode_mismatch")
    _require(
        scope.analysis_timezone == context.analysis_timezone,
        "task_scope_timezone_mismatch",
    )
    _require(
        scope.time_resolution is context.time_resolution
        and scope.time_resolution_source is context.time_resolution_source,
        "task_scope_resolution_mismatch",
    )
    expected_windows = build_time_windows(
        context.time_range,
        context.time_resolution,
        context.analysis_timezone,
    )
    _require(
        tuple(window.time_range for window in scope.time_windows) == expected_windows,
        "task_scope_windows_mismatch",
    )
    expected_segment_indexes = tuple(
        next(
            segment.segment_index
            for segment in context.time_segments
            if window.start >= segment.time_range.start
            and window.end <= segment.time_range.end
        )
        for window in expected_windows
    )
    _require(
        tuple(window.segment_index for window in scope.time_windows)
        == expected_segment_indexes,
        "task_scope_window_segments_mismatch",
    )
    _require(scope.station_refs == node.station_refs, "task_scope_stations_mismatch")
    _require(scope.geometry_ref == node.geometry_ref, "task_scope_geometry_mismatch")
    direct_upstreams = tuple(
        sorted(
            edge.upstream_comid
            for edge in topology.edges
            if edge.downstream_comid == scope.agent_comid and edge.participating
        )
    )
    _require(
        scope.direct_upstream_comids == direct_upstreams,
        "task_scope_upstreams_mismatch",
    )


def _validate_dependency_manifest(
    task: WaterbodyAgentTask,
    manifest: WaterbodyDependencyManifest,
) -> None:
    _require(manifest.run_id == task.run_id, "task_dependency_run_mismatch")
    _require(
        manifest.session_id == task.session_id,
        "task_dependency_session_mismatch",
    )
    _require(
        manifest.target_comid == task.target_comid,
        "task_dependency_comid_mismatch",
    )
    _require(
        str(manifest.topology_ref.item_id) == task.topology_ref.item_id,
        "task_dependency_topology_mismatch",
    )
    _require(
        tuple(item.upstream_comid for item in manifest.dependencies)
        == task.evidence_scope.direct_upstream_comids,
        "task_dependency_scope_mismatch",
    )


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise WaterbodyTaskValidationError(code)
