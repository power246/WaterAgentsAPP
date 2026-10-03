from collections.abc import Callable
from uuid import UUID

from app.application.analysis_runs.topology_driven_workflow import (
    AwaitRunSummaryBoundary,
    ProductionTopologyNodeRunnerFactory,
    TopologyWaterbodyExecutionStage,
)
from app.application.analysis_runs.topology_result_assembler import (
    TopologyResultAssembler,
)
from app.application.analysis_runs.topology_scheduler import (
    WaterbodyWorkflowLimiter,
)
from app.application.analysis_runs.workflow import WorkflowOperations
from app.application.evidence_tools import (
    RunLocalUpstreamResultRegistry,
    UpstreamInferenceResultReader,
)
from app.domain import ModelCatalogSnapshot, ParameterCapabilityRegistry
from app.infrastructure.config import AppSettings
from app.infrastructure.runtime.m5 import M5Runtime
from app.infrastructure.runtime.m6 import M6Runtime
from app.ports.clock import ClockPort


def compose_m7_execution_stage(
    settings: AppSettings,
    *,
    operations: WorkflowOperations,
    m5_runtime: M5Runtime,
    m6_runtime: M6Runtime,
    upstream_results: RunLocalUpstreamResultRegistry,
    result_reader: UpstreamInferenceResultReader,
    capability_registry: ParameterCapabilityRegistry,
    model_catalog: ModelCatalogSnapshot,
    clock: ClockPort,
    result_id_factory: Callable[[], str],
    error_id_factory: Callable[[], UUID],
) -> TopologyWaterbodyExecutionStage:
    assembler = TopologyResultAssembler(result_reader)
    node_runner_factory = ProductionTopologyNodeRunnerFactory(
        operations=operations,
        acquisition=m5_runtime.acquisition,
        session_reader=m5_runtime.transcript,
        waterbody_runner=m6_runtime.runner,
        result_assembler=assembler,
        capability_registry=capability_registry,
        model_catalog=model_catalog,
        result_id_factory=result_id_factory,
        error_id_factory=error_id_factory,
        clock=clock,
    )
    return TopologyWaterbodyExecutionStage(
        operations=operations,
        outcome_stores=upstream_results,
        node_runner_factory=node_runner_factory,
        result_assembler=assembler,
        limiter=WaterbodyWorkflowLimiter(
            settings.waterbody_workflow_max_concurrency
        ),
        summary_boundary=AwaitRunSummaryBoundary(),
    )
