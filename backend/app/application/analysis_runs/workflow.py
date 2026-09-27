import asyncio
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from app.application.analysis_runs.models import (
    AgentRole,
    AnalysisRunSnapshot,
    OperationIdentity,
    PublicError,
    SessionSummary,
    TranscriptItemIdentity,
)
from app.application.transcripts.models import AnalysisRequestV1


class WorkflowStopped(Exception):
    pass


class WorkflowFailed(Exception):
    def __init__(
        self,
        message: str = "The analysis workflow failed.",
        *,
        code: str = "workflow_failed",
        public_message: str = "The analysis workflow failed.",
        failed_stage: str = "analysis_workflow",
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.public_message = public_message
        self.failed_stage = failed_stage
        self.retryable = retryable
        super().__init__(message)


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


class WorkflowOperations(Protocol):
    async def prepare_agent_session(
        self,
        run_id: UUID | str,
        *,
        agent_role: AgentRole | str,
        target_comid: str | None = None,
    ) -> SessionSummary: ...

    async def mark_agent_session_running(
        self,
        run_id: UUID | str,
        session_id: UUID | str,
    ) -> SessionSummary: ...

    async def complete_agent_session(
        self,
        run_id: UUID | str,
        session_id: UUID | str,
    ) -> SessionSummary: ...

    async def fail_agent_session(
        self,
        run_id: UUID | str,
        session_id: UUID | str,
        error: PublicError,
    ) -> SessionSummary: ...

    async def create_operation(
        self,
        run_id: UUID | str,
        session_id: UUID | str,
    ) -> OperationIdentity: ...

    async def reserve_transcript_item(
        self,
        run_id: UUID | str,
        *,
        session_id: UUID | str | None = None,
        request_id: UUID | str | None = None,
        operation_id: UUID | str | None = None,
    ) -> TranscriptItemIdentity: ...

    async def finalize_transcript_item(
        self,
        run_id: UUID | str,
        item_id: UUID | str,
        content_type: str,
        content_schema_version: int,
        body: object,
        *,
        private_values: tuple[str, ...] = (),
    ) -> TranscriptItemIdentity: ...

    async def start_transcript_message(
        self,
        run_id: UUID | str,
        session_id: UUID | str,
        content_type: str,
        content_schema_version: int,
        *,
        request_id: UUID | str | None = None,
        operation_id: UUID | str | None = None,
        tags: tuple[str, ...] = (),
        rag_eligible: bool = False,
        private_values: tuple[str, ...] = (),
    ) -> TranscriptItemIdentity: ...

    async def append_transcript_message_delta(
        self,
        run_id: UUID | str,
        item_id: UUID | str,
        text: str,
    ) -> str: ...

    async def finalize_transcript_message(
        self,
        run_id: UUID | str,
        item_id: UUID | str,
        body: object,
    ) -> TranscriptItemIdentity: ...


@dataclass(frozen=True)
class WorkflowContext:
    run: AnalysisRunSnapshot
    query: str = field(repr=False)
    cancellation: CancellationToken
    request: AnalysisRequestV1 = field(repr=False)
    operations: WorkflowOperations = field(repr=False)


class AnalysisWorkflow(Protocol):
    async def run(self, context: WorkflowContext) -> None: ...
