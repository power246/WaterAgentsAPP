from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

from app.application.analysis_runs.models import PublicError
from app.application.analysis_runs.workflow import WorkflowOperations
from app.application.canonical_json import canonical_json_digest
from app.application.reasoning.model_models import (
    RefusalCompleted,
    StructuredOutputCompleted,
    Terminal,
    ToolCallCompleted,
    UsageReported,
    VisibleTextDelta,
    WaterbodyFinishReason,
    WaterbodyModelEvent,
    parse_waterbody_model_event,
)
from app.application.run_summary.models import (
    RunSummaryAgentMessage,
    RunSummaryFinishReason,
    RunSummaryInput,
    RunSummaryModelRequest,
    RunSummaryModelResponse,
    RunSummaryResult,
)
from app.application.run_summary.runner import RunSummaryModelPort
from app.application.transcripts.models import (
    GenerationStatus,
    MessageEndReason,
    TranscriptContentRef,
)
from app.application.transcripts.redaction import redact
from app.domain import ModelUsageSummary


class RunSummaryTranscriptStore:
    """Store model audit data and publish only validated summary content."""

    def __init__(
        self,
        *,
        operations: WorkflowOperations,
        run_id: UUID,
        session_id: UUID,
        private_values: tuple[str, ...] = (),
    ) -> None:
        self._operations = operations
        self.run_id = run_id
        self.session_id = session_id
        self._private_values = private_values

    async def write_input(self, value: RunSummaryInput) -> TranscriptContentRef:
        if value.run_id != self.run_id or value.session_id != self.session_id:
            raise ValueError("summary transcript scope does not match its input")
        return await self._write("run_summary_input", value)

    async def write_model_request(
        self,
        value: RunSummaryModelRequest,
    ) -> TranscriptContentRef:
        self._require_scope(value.run_id, value.session_id)
        safe_values = redact(
            value.model_dump(mode="json", exclude={"request_digest"}),
            self._private_values,
        )
        if not isinstance(safe_values, dict):
            raise TypeError("redacted run summary request must remain an object")
        safe_value = RunSummaryModelRequest(
            **safe_values,
            request_digest=canonical_json_digest(safe_values),
        )
        return await self._write(
            "run_summary_model_request",
            safe_value,
            operation_id=value.operation_id,
        )

    async def write_model_response(
        self,
        value: RunSummaryModelResponse,
    ) -> TranscriptContentRef:
        self._require_scope(value.run_id, value.session_id)
        return await self._write(
            "run_summary_model_response",
            value,
            operation_id=value.operation_id,
        )

    async def write_result(
        self,
        value: RunSummaryResult,
        *,
        operation_id: UUID,
        message_id: str,
    ) -> TranscriptContentRef:
        self._require_scope(value.run_id, value.session_id)
        message = RunSummaryAgentMessage(
            run_id=self.run_id,
            session_id=self.session_id,
            message_id=message_id,
            operation_id=operation_id,
            text=value.plain_language_summary,
            generation_status=GenerationStatus.COMPLETED,
            end_reason=MessageEndReason.COMPLETED,
        )
        summary = await self._operations.complete_run_summary_agent_session(
            self.run_id,
            self.session_id,
            value,
            message=message,
        )
        if summary.result_ref is None:
            raise RuntimeError("completed run summary session has no result reference")
        return TranscriptContentRef(
            run_id=summary.result_ref.run_id,
            item_id=summary.result_ref.item_id,
        )

    async def fail(self, error: PublicError):
        return await self._operations.fail_agent_session(
            self.run_id,
            self.session_id,
            error,
        )

    async def cancel(self):
        return await self._operations.cancel_agent_session(
            self.run_id,
            self.session_id,
        )

    def recording_port(self, delegate: RunSummaryModelPort) -> RunSummaryModelPort:
        return TranscriptRecordingRunSummaryModelPort(delegate=delegate, store=self)

    async def _write(
        self,
        content_type: str,
        body: object,
        *,
        operation_id: UUID | None = None,
    ) -> TranscriptContentRef:
        identity = await self._operations.reserve_transcript_item(
            self.run_id,
            session_id=self.session_id,
            operation_id=operation_id,
        )
        await self._operations.finalize_transcript_item(
            self.run_id,
            identity.item_id,
            content_type,
            1,
            body,
            private_values=self._private_values,
        )
        return TranscriptContentRef(run_id=self.run_id, item_id=identity.item_id)

    def _require_scope(self, run_id: UUID, session_id: UUID) -> None:
        if run_id != self.run_id or session_id != self.session_id:
            raise ValueError("summary transcript content escaped its session")


class TranscriptRecordingRunSummaryModelPort:
    def __init__(
        self,
        *,
        delegate: RunSummaryModelPort,
        store: RunSummaryTranscriptStore,
    ) -> None:
        self._delegate = delegate
        self._store = store

    async def stream(
        self,
        request: RunSummaryModelRequest,
    ) -> AsyncIterator[WaterbodyModelEvent]:
        await self._store.write_model_request(request)
        visible: list[str] = []
        structured: StructuredOutputCompleted | None = None
        usage: ModelUsageSummary | None = None
        terminal: Terminal | None = None
        refusal = False
        unexpected_tool = False
        response_written = False

        async def write_response(
            reason: RunSummaryFinishReason,
            provider_response_id: str | None,
        ) -> None:
            nonlocal response_written
            if response_written:
                return
            if (
                unexpected_tool
                or refusal
                or (
                    reason is RunSummaryFinishReason.COMPLETED
                    and structured is None
                )
            ):
                reason = RunSummaryFinishReason.ERROR
            await self._store.write_model_response(
                RunSummaryModelResponse(
                    run_id=self._store.run_id,
                    session_id=self._store.session_id,
                    operation_id=request.operation_id,
                    model=request.model,
                    finish_reason=reason,
                    message_refs=(),
                    structured_output=(
                        structured.value
                        if structured is not None
                        and reason is RunSummaryFinishReason.COMPLETED
                        else None
                    ),
                    visible_text="".join(visible),
                    usage=usage,
                    provider_response_id=provider_response_id,
                )
            )
            response_written = True

        try:
            async for raw in self._delegate.stream(request):
                event = parse_waterbody_model_event(raw)
                matches = (
                    event.operation_id == request.operation_id
                    and event.model == request.model
                )
                if matches and isinstance(event, VisibleTextDelta):
                    visible.append(event.text)
                elif matches and isinstance(event, RefusalCompleted):
                    refusal = True
                    visible.append(event.text)
                elif matches and isinstance(event, ToolCallCompleted):
                    unexpected_tool = True
                elif matches and isinstance(event, StructuredOutputCompleted):
                    if structured is None:
                        structured = event
                elif matches and isinstance(event, UsageReported):
                    if usage is None:
                        usage = ModelUsageSummary(
                            input_tokens=event.input_tokens,
                            output_tokens=event.output_tokens,
                        )
                elif matches and isinstance(event, Terminal):
                    terminal = event
                    await write_response(
                        RunSummaryFinishReason(event.finish_reason.value),
                        event.provider_response_id,
                    )
                yield event
        finally:
            if not response_written:
                await write_response(
                    RunSummaryFinishReason.ERROR,
                    terminal.provider_response_id if terminal is not None else None,
                )
