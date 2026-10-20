from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Protocol
from uuid import UUID

from app.application.analysis_runs.cancellation import await_with_cancellation
from app.application.analysis_runs.workflow import (
    AnalysisWorkflow,
    WorkflowContext,
    WorkflowFailed,
    WorkflowStopped,
)
from app.application.hydrology_topology import TopologyInvariantError
from app.application.query_understanding import AnalysisContext
from app.domain import HydrologyGraph
from app.ports import PortError

logger = logging.getLogger("wateragents.query_topology")


class QueryUnderstandingStage(Protocol):
    async def understand(self, context: WorkflowContext) -> AnalysisContext: ...


class TopologyBuildingStage(Protocol):
    async def build(self, context: AnalysisContext) -> HydrologyGraph: ...


class TopologyExecutionStage(Protocol):
    async def execute(
        self,
        context: WorkflowContext,
        analysis_context: AnalysisContext,
        graph: HydrologyGraph,
        *,
        topology_item_id: UUID,
    ) -> None: ...


class QueryTopologyWorkflow(AnalysisWorkflow):
    """Own query, topology, and the once-bound downstream execution stage."""

    def __init__(
        self,
        query_understanding: QueryUnderstandingStage,
        topology_builder: TopologyBuildingStage,
    ) -> None:
        self._query_understanding = query_understanding
        self._topology_builder = topology_builder
        self._execution_stage: TopologyExecutionStage | None = None
        self._guideline_pinner = None

    def bind_guidelines(self, pinner) -> None:
        if self._guideline_pinner is not None:
            raise RuntimeError("guideline pinning is already bound")
        self._guideline_pinner = pinner

    @property
    def execution_bound(self) -> bool:
        return self._execution_stage is not None

    def bind_execution_stage(self, stage: TopologyExecutionStage) -> None:
        if self._execution_stage is not None:
            raise RuntimeError("topology execution stage is already bound")
        self._execution_stage = stage

    async def run(self, context: WorkflowContext) -> None:
        analysis_context = await self._query_understanding.understand(context)
        context.cancellation.checkpoint()
        try:
            graph = await await_with_cancellation(
                self._topology_builder.build(analysis_context),
                context.cancellation,
            )
        except (WorkflowStopped, asyncio.CancelledError):
            raise
        except TopologyInvariantError as error:
            raise WorkflowFailed(
                "The hydrology topology was invalid.",
                code="topology_invalid",
                public_message="The hydrology topology response was invalid.",
                failed_stage="topology_building",
                retryable=False,
            ) from error
        except PortError as error:
            availability = error.operation == "check_availability"
            logger.warning(
                "Topology dependency failed for run %s: %s",
                context.run.run_id,
                error,
                extra={
                    "event_name": "topology_dependency_failed",
                    "run_id": context.run.run_id,
                    "target_comid": analysis_context.target.comid,
                    "operation": error.operation,
                    "provider": error.provider,
                    "provider_error": type(error).__name__,
                    "provider_status": error.status_code,
                    "provider_retry_after_seconds": getattr(
                        error, "retry_after_seconds", None
                    ),
                },
            )
            raise WorkflowFailed(
                "A hydrology topology dependency was unavailable.",
                code=(
                    "station_availability_failed"
                    if availability
                    else "topology_lookup_failed"
                ),
                public_message=(
                    "Station availability could not be checked."
                    if availability
                    else "The hydrology network could not be queried."
                ),
                failed_stage="topology_building",
                retryable=True,
            ) from None
        except Exception as error:
            raise WorkflowFailed(
                "The hydrology topology could not be constructed.",
                code="topology_invalid",
                public_message="The hydrology topology could not be constructed.",
                failed_stage="topology_building",
                retryable=False,
            ) from error

        context.cancellation.checkpoint()
        topology_item = await context.operations.reserve_transcript_item(
            context.run.run_id,
        )
        context.cancellation.checkpoint()
        await context.operations.finalize_transcript_item(
            context.run.run_id,
            topology_item.item_id,
            "topology_ready",
            1,
            graph,
        )
        context.cancellation.checkpoint()
        if self._execution_stage is None:
            raise WorkflowFailed(
                "The topology execution stage is not configured.",
                code="workflow_not_configured",
                public_message="The analysis workflow is not configured.",
                failed_stage="waterbody_execution",
                retryable=False,
            )
        if self._guideline_pinner is not None:
            guidelines = await self._guideline_pinner(context.run.run_id, context.cancellation)
            context.cancellation.checkpoint()
            await guidelines.save(context.operations)
            context = replace(context, guidelines=guidelines)
        await self._execution_stage.execute(
            context,
            analysis_context,
            graph,
            topology_item_id=topology_item.item_id,
        )
