import asyncio
from dataclasses import dataclass, field
from typing import Protocol

from app.application.analysis_runs.models import AnalysisRunSnapshot


class WorkflowStopped(Exception):
    pass


class WorkflowFailed(Exception):
    pass


class CancellationToken:
    def __init__(self, stop_requested: asyncio.Event) -> None:
        self._stop_requested = stop_requested

    def is_requested(self) -> bool:
        return self._stop_requested.is_set()

    def checkpoint(self) -> None:
        if self.is_requested():
            raise WorkflowStopped()

    async def wait(self) -> None:
        await self._stop_requested.wait()


@dataclass(frozen=True)
class WorkflowContext:
    run: AnalysisRunSnapshot
    query: str = field(repr=False)
    cancellation: CancellationToken


class AnalysisWorkflow(Protocol):
    async def run(self, context: WorkflowContext) -> None: ...


class DefaultFakeWorkflow:
    async def run(self, context: WorkflowContext) -> None:
        context.cancellation.checkpoint()
