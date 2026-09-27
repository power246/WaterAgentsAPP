from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Protocol, TypeVar

from app.application.analysis_runs.workflow import (
    AnalysisWorkflow,
    CancellationToken,
    WorkflowContext,
    WorkflowFailed,
    WorkflowStopped,
)
from app.application.hydrology_topology import TopologyInvariantError
from app.application.query_understanding import AnalysisContext
from app.domain import HydrologyGraph
from app.ports import PortError


class QueryUnderstandingStage(Protocol):
    async def understand(self, context: WorkflowContext) -> AnalysisContext: ...


class TopologyBuildingStage(Protocol):
    async def build(self, context: AnalysisContext) -> HydrologyGraph: ...


ResultT = TypeVar("ResultT")


class QueryTopologyWorkflow(AnalysisWorkflow):
    """Runs the M4 query-understanding and topology stages in one owned task."""

    def __init__(
        self,
        query_understanding: QueryUnderstandingStage,
        topology_builder: TopologyBuildingStage,
    ) -> None:
        self._query_understanding = query_understanding
        self._topology_builder = topology_builder

    async def run(self, context: WorkflowContext) -> None:
        analysis_context = await self._query_understanding.understand(context)
        context.cancellation.checkpoint()
        try:
            graph = await self._await_with_cancellation(
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

    @staticmethod
    async def _await_with_cancellation(
        awaitable: Awaitable[ResultT],
        cancellation: CancellationToken,
    ) -> ResultT:
        work = asyncio.ensure_future(awaitable)
        stopped = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                (work, stopped),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation.is_requested() or stopped in done:
                raise WorkflowStopped()
            return work.result()
        finally:
            for task in (work, stopped):
                if not task.done():
                    task.cancel()
            await asyncio.gather(work, stopped, return_exceptions=True)
