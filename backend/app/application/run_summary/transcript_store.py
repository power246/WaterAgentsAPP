from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from app.application.analysis_runs.models import PublicError, TranscriptItemIdentity
from app.application.analysis_runs.workflow import WorkflowOperations
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
from app.application.transcripts.models import (
    GenerationStatus,
    MessageEndReason,
    TranscriptContentRef,
)
from app.domain import ModelUsageSummary
from app.application.run_summary.runner import RunSummaryModelPort


class RunSummaryTranscriptStore:
    """Use the existing workflow writer and visible-message stream for M6."""

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
        return await self._write(
            "run_summary_model_request",
            value,
            operation_id=value.operation_id,
        )

    async def start_agent_message(
        self,
        request: RunSummaryModelRequest,
        message_id: str,
    ) -> TranscriptItemIdentity:
        return await self._operations.start_transcript_message(
            self.run_id,
            self.session_id,
            "run_summary_agent_message",
            1,
            operation_id=request.operation_id,
            private_values=self._private_values,
            initial_body=RunSummaryAgentMessage(
                run_id=self.run_id,
                session_id=self.session_id,
                message_id=message_id,
                operation_id=request.operation_id,
                text="",
                generation_status=GenerationStatus.GENERATING,
            ),
        )

    async def append_agent_message_delta(
        self,
        identity: TranscriptItemIdentity,
        text: str,
    ) -> str:
        return await self._operations.append_transcript_message_delta(
            self.run_id,
            identity.item_id,
            text,
        )

    async def finalize_agent_message(
        self,
        identity: TranscriptItemIdentity,
        request: RunSummaryModelRequest,
        *,
        message_id: str,
        text: str,
        completed: bool,
        end_reason: MessageEndReason,
    ) -> TranscriptContentRef:
        await self._operations.finalize_transcript_message(
            self.run_id,
            identity.item_id,
            RunSummaryAgentMessage(
                run_id=self.run_id,
                session_id=self.session_id,
                message_id=message_id,
                operation_id=request.operation_id,
                text=text,
                generation_status=(
                    GenerationStatus.COMPLETED
                    if completed
                    else GenerationStatus.INCOMPLETE
                ),
                end_reason=end_reason,
            ),
        )
        return TranscriptContentRef(run_id=self.run_id, item_id=identity.item_id)

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

    async def write_result(self, value: RunSummaryResult) -> TranscriptContentRef:
        self._require_scope(value.run_id, value.session_id)
        return await self._write("run_summary_result", value)

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


@dataclass(slots=True)
class _OpenMessage:
    identity: TranscriptItemIdentity
    message_id: str
    text: str = ""


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
        messages: dict[str, _OpenMessage] = {}
        message_refs: list[TranscriptContentRef] = []
        structured: StructuredOutputCompleted | None = None
        usage: ModelUsageSummary | None = None
        terminal: Terminal | None = None
        refusal = False
        unexpected_tool = False
        response_written = False

        async def append(message_id: str, text: str) -> None:
            message = messages.get(message_id)
            if message is None:
                identity = await self._store.start_agent_message(request, message_id)
                message = _OpenMessage(identity, message_id)
                messages[message_id] = message
            await self._store.append_agent_message_delta(message.identity, text)
            message.text += text

        async def finish(reason: RunSummaryFinishReason) -> None:
            completed = reason is RunSummaryFinishReason.COMPLETED
            end_reason = (
                MessageEndReason.COMPLETED
                if completed
                else MessageEndReason.TRUNCATED
                if reason in {
                    RunSummaryFinishReason.TRUNCATED,
                    RunSummaryFinishReason.INCOMPLETE,
                }
                else MessageEndReason.CANCELLED
                if reason is RunSummaryFinishReason.CANCELLED
                else MessageEndReason.ERROR
            )
            for message in messages.values():
                message_refs.append(
                    await self._store.finalize_agent_message(
                        message.identity,
                        request,
                        message_id=message.message_id,
                        text=message.text,
                        completed=completed,
                        end_reason=end_reason,
                    )
                )

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
            await finish(reason)
            await self._store.write_model_response(
                RunSummaryModelResponse(
                    run_id=self._store.run_id,
                    session_id=self._store.session_id,
                    operation_id=request.operation_id,
                    model=request.model,
                    finish_reason=reason,
                    message_refs=tuple(message_refs),
                    structured_output=(
                        structured.value
                        if structured is not None
                        and reason is RunSummaryFinishReason.COMPLETED
                        else None
                    ),
                    visible_text="".join(message.text for message in messages.values()),
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
                    await append(event.message_id, event.text)
                elif matches and isinstance(event, RefusalCompleted):
                    refusal = True
                    await append(event.message_id, event.text)
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
