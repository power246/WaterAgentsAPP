import asyncio
from datetime import datetime, timezone
from uuid import UUID

from app.application.analysis_runs.models import (
    AgentRole,
    AgentSessionStatus,
    AnalysisConfigurationSnapshot,
    AnalysisRunSnapshot,
    RunStatus,
    SessionResultReference,
    SessionSummary,
    WaterbodySessionResultSummary,
)
from app.application.analysis_runs.node_launch import (
    NodeLaunchAction,
    NodeLaunchPolicy,
    build_node_launch_facts,
)
from app.application.analysis_runs.topology_result_models import (
    DependencyStatus,
    WaterbodyDependency,
    WaterbodyDependencyManifest,
)
from app.application.analysis_runs.zero_model_result import (
    DeterministicInsufficientResultAssembler,
)
from app.application.analysis_runs.workflow import CancellationToken
from app.application.canonical_json import canonical_json_digest
from app.application.evidence_tools import assemble_evidence_session_scope
from app.application.reasoning.task import (
    ValidatedWaterbodyAgentTask,
    WaterbodyAgentTask,
)
from app.application.transcripts.models import TranscriptContentRef
from app.domain import (
    IndeterminateReason,
    ItemReference,
    MemoryMode,
    OutcomeStatus,
    ReferenceScope,
    TimeResolutionPreference,
    TimeResolutionPreferenceSource,
)
from app.infrastructure.config import load_parameter_capability_registry
from tests.application.evidence_tools.conftest import (
    make_context, make_discharge_context, make_graph,
)
from tests.application.evidence_tools.fakes import MemoryEvidenceStore
from tests.application.evidence_tools.test_acquisition_workflow import (
    _runtime,
    _upstream_port,
    _water_port,
)


RUN_ID = UUID("00000000-0000-0000-0000-000000000100")
SESSION_ID = UUID("00000000-0000-0000-0000-000000000500")
NOW = datetime(2026, 10, 3, 8, tzinfo=timezone.utc)


class FixedClock:
    def now(self):
        return NOW


class RecordingTranscriptStore:
    def __init__(self) -> None:
        self.inputs = []
        self.results = []

    async def write_agent_input(self, task, *, initial_evidence_bundle_ref=None):
        self.inputs.append((task, initial_evidence_bundle_ref))

    async def complete_with_result(self, result):
        self.results.append(result)
        return SessionSummary(
            session_id=SESSION_ID,
            agent_role=AgentRole.WATERBODY,
            target_comid="123",
            status=AgentSessionStatus.COMPLETED,
            created_at=NOW,
            updated_at=NOW,
            result_ref=SessionResultReference(
                run_id=RUN_ID,
                item_id=UUID("00000000-0000-0000-0000-000000000600"),
            ),
            result_summary=WaterbodySessionResultSummary(
                result_id=result.result_id,
                target_comid=result.target_comid,
                risk_level=result.overall_assessment.risk_level,
                availability=result.overall_assessment.availability,
                overall_confidence=result.overall_confidence,
            ),
        )


def dependency_manifest(topology_item_id: UUID) -> WaterbodyDependencyManifest:
    values = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "session_id": SESSION_ID,
        "target_comid": "123",
        "topology_ref": TranscriptContentRef(
            run_id=RUN_ID,
            item_id=topology_item_id,
        ),
        "dependencies": (
            WaterbodyDependency(
                upstream_comid="200",
                upstream_session_id=UUID(
                    "00000000-0000-0000-0000-000000000200"
                ),
                dependency_status=DependencyStatus.INSUFFICIENT,
            ),
        ),
    }
    return WaterbodyDependencyManifest(
        **values,
        manifest_digest=canonical_json_digest(values),
    )


def validated_task(scope, context, graph) -> ValidatedWaterbodyAgentTask:
    topology_item_id = UUID(scope.topology_ref.item_id)
    session = SessionSummary(
        session_id=SESSION_ID,
        agent_role=AgentRole.WATERBODY,
        target_comid="123",
        status=AgentSessionStatus.RUNNING,
        created_at=NOW,
        updated_at=NOW,
    )
    task = WaterbodyAgentTask(
        run_id=RUN_ID,
        session_id=SESSION_ID,
        target_comid="123",
        analysis_context_ref=ItemReference(item_id=scope.analysis_context_ref.item_id),
        topology_ref=ItemReference(item_id=scope.topology_ref.item_id),
        evidence_scope=scope,
        waterbody_model=context.waterbody_model,
        memory_mode=MemoryMode.STATELESS,
        cancellation_token=scope.cancel_token,
        dependency_manifest=dependency_manifest(topology_item_id),
    )
    return ValidatedWaterbodyAgentTask(
        task=task,
        run=AnalysisRunSnapshot(
            run_id=RUN_ID,
            status=RunStatus.RUNNING,
            created_at=NOW,
            updated_at=NOW,
            configuration=AnalysisConfigurationSnapshot(
                analysis_timezone=context.analysis_timezone,
                time_resolution_preference=TimeResolutionPreference.AUTO,
                time_resolution_preference_source=(
                    TimeResolutionPreferenceSource.FRONTEND
                ),
                parser_model=context.parser_model,
                waterbody_model=context.waterbody_model,
                memory_mode=context.memory_mode,
            ),
        ),
        session=session,
        context=context,
        topology=graph,
    )


def test_explicit_discharge_target_is_usable_for_node_launch() -> None:
    async def exercise() -> None:
        context = make_discharge_context()
        graph = make_graph()
        scope = assemble_evidence_session_scope(
            run_id=str(RUN_ID),
            session_id=str(SESSION_ID),
            session_fact=ReferenceScope(
                item_id=str(SESSION_ID), run_id=str(RUN_ID), session_id=str(SESSION_ID),
            ),
            analysis_context_ref=ReferenceScope(
                item_id=str(UUID(int=700)), run_id=str(RUN_ID),
            ),
            topology_ref=ReferenceScope(
                item_id=str(UUID(int=701)), run_id=str(RUN_ID),
            ),
            context=context,
            topology=graph,
            agent_comid="123",
            registry=load_parameter_capability_registry(),
            cancel_token=CancellationToken(asyncio.Event()),
        )
        store = MemoryEvidenceStore()
        handoff = await _runtime(store, water_port=_water_port(scope)).start(scope)

        facts = build_node_launch_facts(validated_task(scope, context, graph), handoff)
        assert facts.local_target_usable
        assert facts.local_discharge_usable

    asyncio.run(exercise())


def test_stationless_all_unusable_commits_zero_model_result() -> None:
    async def exercise() -> None:
        context = make_context()
        graph = make_graph(with_station=False, with_upstream=True)
        context_item_id = UUID("00000000-0000-0000-0000-000000000700")
        topology_item_id = UUID("00000000-0000-0000-0000-000000000701")
        scope = assemble_evidence_session_scope(
            run_id=str(RUN_ID),
            session_id=str(SESSION_ID),
            session_fact=ReferenceScope(
                item_id=str(SESSION_ID),
                run_id=str(RUN_ID),
                session_id=str(SESSION_ID),
            ),
            analysis_context_ref=ReferenceScope(
                item_id=str(context_item_id),
                run_id=str(RUN_ID),
            ),
            topology_ref=ReferenceScope(
                item_id=str(topology_item_id),
                run_id=str(RUN_ID),
            ),
            context=context,
            topology=graph,
            agent_comid="123",
            registry=load_parameter_capability_registry(),
            cancel_token=CancellationToken(asyncio.Event()),
        )
        evidence_store = MemoryEvidenceStore()
        acquisition = _runtime(
            evidence_store,
            upstream_port=_upstream_port(
                scope,
                evidence_store,
                result_kind="indeterminate",
            ),
            upstream_ready=True,
        )
        handoff = await acquisition.start(scope)
        transcript = RecordingTranscriptStore()
        task = validated_task(scope, context, graph)
        decision = NodeLaunchPolicy().decide(
            build_node_launch_facts(task, handoff),
            task.task.dependency_manifest,
        )

        outcome = await DeterministicInsufficientResultAssembler(
            capability_registry=load_parameter_capability_registry(),
            result_id_factory=lambda: "result:zero-model",
            clock=FixedClock(),
        ).run(
            task,
            handoff,
            transcript_store=transcript,
        )

        assert handoff.stop_before_model is True
        assert decision.action is NodeLaunchAction.DETERMINISTIC_INSUFFICIENT
        assert outcome.planning_calls == outcome.final_calls == 0
        assert outcome.optional_tool_calls == 0
        assert outcome.session.status is AgentSessionStatus.COMPLETED
        assert outcome.session.result_ref is not None
        assert outcome.dependency_outcome.status is DependencyStatus.INSUFFICIENT
        assert outcome.dependency_outcome.usable_cell_refs == ()
        assert len(transcript.inputs) == len(transcript.results) == 1
        assert outcome.result is not None
        assert outcome.result.analysis_detail.transcript_item_refs == tuple(
            sorted(
                {
                    *(item_id for item_id, _ in evidence_store.fragments),
                    *(item_id for _, _, item_id in evidence_store.records),
                }
            )
        )
        execution = outcome.result.model_execution_summary
        assert execution.planning_calls == 0
        assert execution.reasoning_batches == 0
        assert execution.repair_calls == 0
        assert execution.synthesis_calls == 0
        assert execution.usage is None
        points = tuple(
            point for series in outcome.result.series for point in series.points
        )
        assert points
        assert all(
            point.outcome_status is OutcomeStatus.INDETERMINATE
            for point in points
        )
        assert all(point.source is None and point.confidence == 0 for point in points)
        assert all(
            point.assessment.reason_codes
            == (IndeterminateReason.INSUFFICIENT_EVIDENCE,)
            for point in points
        )

    asyncio.run(exercise())
