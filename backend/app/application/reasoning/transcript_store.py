from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from app.application.analysis_runs.models import (
    PublicError,
    SessionSummary,
    TranscriptItemIdentity,
)
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
    WaterbodyModelRequest,
    WaterbodyModelUsage,
    parse_waterbody_model_event,
)
from app.application.reasoning.prompt_policy import canonical_json_digest
from app.application.reasoning.task import ValidatedWaterbodyAgentTask
from app.application.reasoning.transcript_models import (
    ReasoningDecisionType,
    ReasoningDecisionV1,
    StoredModelFinishReason,
    WaterbodyAgentInputV1,
    WaterbodyAgentMessageV1,
    WaterbodyModelRequestV1,
    WaterbodyModelResponseV1,
    stored_finish_reason,
)
from app.application.reasoning.validation_repair import ValidationExchangeAudit
from app.application.transcripts.models import (
    GenerationStatus,
    MessageEndReason,
    TranscriptContentRef,
)
from app.application.transcripts.redaction import redact
from app.domain import InferenceResult, ItemReference
from app.ports.waterbody_model import WaterbodyModelPort


class ReasoningTranscriptStore:
    """Persist M6 content through the one M3 workflow writer."""

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
        self._response_refs: dict[UUID, TranscriptContentRef] = {}
        self._cancellation = None

    async def write_agent_input(
        self,
        task: ValidatedWaterbodyAgentTask,
        *,
        initial_evidence_bundle_ref: ItemReference | None = None,
    ) -> TranscriptContentRef:
        if task.task.run_id != self.run_id or task.task.session_id != self.session_id:
            raise ValueError("reasoning transcript scope does not match the task")
        self._cancellation = task.task.cancellation_token
        scope = task.task.evidence_scope
        body = WaterbodyAgentInputV1(
            run_id=self.run_id,
            session_id=self.session_id,
            target_comid=task.task.target_comid,
            analysis_context_ref=self._run_ref(task.task.analysis_context_ref),
            topology_ref=self._run_ref(task.task.topology_ref),
            initial_evidence_bundle_ref=(
                self._run_ref(initial_evidence_bundle_ref)
                if initial_evidence_bundle_ref is not None
                else None
            ),
            requested_parameters=scope.requested_parameters,
            supporting_parameters=scope.supporting_parameters,
            unit_preferences=task.context.unit_preferences,
            window_ids=tuple(window.window_id for window in scope.time_windows),
            waterbody_model=task.task.waterbody_model,
            memory_mode=task.task.memory_mode,
        )
        return await self._write("waterbody_agent_input", body)

    def recording_port(self, delegate: WaterbodyModelPort) -> WaterbodyModelPort:
        return TranscriptRecordingWaterbodyModelPort(delegate=delegate, store=self)

    async def write_model_request(
        self,
        request: WaterbodyModelRequest,
    ) -> TranscriptContentRef:
        values = {
            "schema_version": 1,
            "operation_id": request.operation_id,
            "phase": request.phase,
            "model": request.model,
            "scope_digest": request.scope_digest,
            "prompt_policy_digest": request.prompt_policy_digest,
            "evidence_bundle_ref": self._run_ref(request.evidence_bundle_ref),
            "batch_id": request.batch_id,
            "system_prompt": request.system_prompt,
            "messages": request.messages,
            "tool_descriptors": request.tool_descriptors,
            "response_schema": request.response_schema,
            "deadline": request.deadline,
        }
        original = WaterbodyModelRequestV1(
            **values,
            request_digest=canonical_json_digest(values),
        )
        safe_values = redact(
            original.model_dump(mode="json", exclude={"request_digest"}),
            self._private_values,
        )
        if not isinstance(safe_values, dict):
            raise TypeError("redacted model request must remain an object")
        body = WaterbodyModelRequestV1(
            **safe_values,
            request_digest=canonical_json_digest(safe_values),
        )
        return await self._write(
            "waterbody_model_request",
            body,
            operation_id=request.operation_id,
        )

    async def start_agent_message(
        self,
        request: WaterbodyModelRequest,
        message_id: str,
    ) -> TranscriptItemIdentity:
        return await self._operations.start_transcript_message(
            self.run_id,
            self.session_id,
            "waterbody_agent_message",
            1,
            operation_id=request.operation_id,
            private_values=self._private_values,
            initial_body=WaterbodyAgentMessageV1(
                message_id=message_id,
                operation_id=request.operation_id,
                phase=request.phase,
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
        request: WaterbodyModelRequest,
        *,
        message_id: str,
        text: str,
        generation_status: GenerationStatus,
        end_reason: MessageEndReason,
    ) -> TranscriptContentRef:
        await self._operations.finalize_transcript_message(
            self.run_id,
            identity.item_id,
            WaterbodyAgentMessageV1(
                message_id=message_id,
                operation_id=request.operation_id,
                phase=request.phase,
                text=text,
                generation_status=generation_status,
                end_reason=end_reason,
            ),
        )
        return TranscriptContentRef(run_id=self.run_id, item_id=identity.item_id)

    async def write_model_response(
        self,
        body: WaterbodyModelResponseV1,
    ) -> TranscriptContentRef:
        reference = await self._write(
            "waterbody_model_response",
            body,
            operation_id=body.operation_id,
        )
        self._response_refs[body.operation_id] = reference
        return reference

    async def write_planning_decision(
        self,
        request: WaterbodyModelRequest,
        *,
        decision_type: ReasoningDecisionType,
        bundle_ref: ItemReference,
        rationale: str | None,
        tool_call_ids: tuple[str, ...] = (),
    ) -> TranscriptContentRef:
        response_ref = self._response_refs.get(request.operation_id)
        if response_ref is None:
            raise ValueError("planning decision requires its stored model response")
        return await self._write(
            "reasoning_decision",
            ReasoningDecisionV1(
                decision_type=decision_type,
                operation_id=request.operation_id,
                phase=request.phase,
                model_response_ref=response_ref,
                bundle_ref=self._run_ref(bundle_ref),
                scope_digest=request.scope_digest,
                rationale=rationale,
                tool_call_ids=tool_call_ids,
            ),
            operation_id=request.operation_id,
        )

    async def write_validated_exchange(
        self,
        audit: ValidationExchangeAudit,
    ) -> TranscriptContentRef | None:
        accepted = audit.accepted_operation_id
        if accepted is None or audit.validated_digest is None:
            return None
        if accepted == audit.initial_request.operation_id:
            request = audit.initial_request
            response = audit.initial_response
            repair_of = None
        else:
            request = audit.repair_request
            response = audit.repair_response
            repair_of = audit.initial_request.operation_id
        if request is None or response is None:
            raise ValueError("accepted validation exchange is incomplete")
        response_ref = self._response_refs.get(request.operation_id)
        if response_ref is None:
            raise ValueError("validated decision requires its stored model response")
        output = next(
            (
                event
                for event in response.events
                if isinstance(event, StructuredOutputCompleted)
            ),
            None,
        )
        if output is None:
            raise ValueError("validated decision requires structured output")
        decision_type = (
            ReasoningDecisionType.REASONING_BATCH_VALIDATED
            if audit.original_phase.value == "reasoning_batch"
            else ReasoningDecisionType.OVERALL_SYNTHESIS_VALIDATED
        )
        return await self._write(
            "reasoning_decision",
            ReasoningDecisionV1(
                decision_type=decision_type,
                operation_id=request.operation_id,
                phase=request.phase,
                model_response_ref=response_ref,
                bundle_ref=self._run_ref(request.evidence_bundle_ref),
                batch_id=request.batch_id,
                scope_digest=request.scope_digest,
                accepted_output=output.value,
                validated_digest=audit.validated_digest,
                repair_of_operation_id=repair_of,
                validation_issue_codes=tuple(
                    dict.fromkeys(issue.code for issue in audit.initial_issues)
                ),
                unit_decisions=audit.unit_decisions,
            ),
            operation_id=request.operation_id,
        )

    async def complete_with_result(self, result: InferenceResult) -> SessionSummary:
        return await self._operations.complete_waterbody_agent_session(
            self.run_id,
            self.session_id,
            result,
        )

    async def fail(self, error: PublicError) -> SessionSummary:
        return await self._operations.fail_agent_session(
            self.run_id,
            self.session_id,
            error,
        )

    async def cancel(self) -> SessionSummary:
        return await self._operations.cancel_agent_session(
            self.run_id,
            self.session_id,
        )

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

    def _run_ref(self, reference: ItemReference) -> TranscriptContentRef:
        return TranscriptContentRef(
            run_id=self.run_id,
            item_id=UUID(reference.item_id),
        )


@dataclass(slots=True)
class _OpenMessage:
    identity: TranscriptItemIdentity
    message_id: str
    text: str = ""


class TranscriptRecordingWaterbodyModelPort:
    def __init__(
        self,
        *,
        delegate: WaterbodyModelPort,
        store: ReasoningTranscriptStore,
    ) -> None:
        self._delegate = delegate
        self._store = store

    async def stream(
        self,
        request: WaterbodyModelRequest,
    ) -> AsyncIterator[WaterbodyModelEvent]:
        await self._store.write_model_request(request)
        messages: dict[str, _OpenMessage] = {}
        message_refs: list[TranscriptContentRef] = []
        tool_calls: list[ToolCallCompleted] = []
        outputs: list[StructuredOutputCompleted] = []
        refusal: RefusalCompleted | None = None
        usage: WaterbodyModelUsage | None = None
        terminal: Terminal | None = None
        response_written = False
        messages_finished = False

        async def append_message(message_id: str, text: str) -> None:
            message = messages.get(message_id)
            if message is None:
                identity = await self._store.start_agent_message(request, message_id)
                message = _OpenMessage(identity=identity, message_id=message_id)
                messages[message_id] = message
            await self._store.append_agent_message_delta(
                message.identity,
                text,
            )
            message.text += text

        async def finish_messages(reason: StoredModelFinishReason) -> None:
            nonlocal messages_finished
            if messages_finished:
                return
            if not messages and reason in {
                StoredModelFinishReason.CONTENT_FILTERED,
                StoredModelFinishReason.REFUSED,
                StoredModelFinishReason.TRUNCATED,
                StoredModelFinishReason.INCOMPLETE,
                StoredModelFinishReason.CANCELLED,
                StoredModelFinishReason.ERROR,
            }:
                message_id = f"message:{request.operation_id}"
                identity = await self._store.start_agent_message(request, message_id)
                messages[message_id] = _OpenMessage(identity, message_id)
            generation_status = (
                GenerationStatus.COMPLETED
                if reason is StoredModelFinishReason.COMPLETED
                else GenerationStatus.INCOMPLETE
            )
            end_reason = (
                MessageEndReason.COMPLETED
                if reason is StoredModelFinishReason.COMPLETED
                else MessageEndReason.TRUNCATED
                if reason in {
                    StoredModelFinishReason.TRUNCATED,
                    StoredModelFinishReason.INCOMPLETE,
                }
                else MessageEndReason.CANCELLED
                if reason is StoredModelFinishReason.CANCELLED
                else MessageEndReason.ERROR
            )
            for message in messages.values():
                reference = await self._store.finalize_agent_message(
                    message.identity,
                    request,
                    message_id=message.message_id,
                    text=message.text,
                    generation_status=generation_status,
                    end_reason=end_reason,
                )
                message_refs.append(reference)
            messages_finished = True

        async def write_response(
            reason: StoredModelFinishReason,
            provider_response_id: str | None,
        ) -> None:
            nonlocal response_written
            if response_written:
                return
            stored_reason = (
                StoredModelFinishReason.ERROR
                if reason is StoredModelFinishReason.REFUSED and refusal is None
                else reason
            )
            await finish_messages(stored_reason)
            await self._store.write_model_response(
                WaterbodyModelResponseV1(
                    operation_id=request.operation_id,
                    phase=request.phase,
                    model=request.model,
                    finish_reason=stored_reason,
                    message_refs=tuple(message_refs),
                    tool_calls=tuple(tool_calls),
                    structured_outputs=tuple(outputs),
                    refusal=refusal,
                    usage=usage,
                    provider_response_id=provider_response_id,
                )
            )
            response_written = True

        try:
            async for raw_event in self._delegate.stream(request):
                event = parse_waterbody_model_event(raw_event)
                matching = (
                    event.operation_id == request.operation_id
                    and event.model == request.model
                )
                if matching and isinstance(event, VisibleTextDelta):
                    await append_message(event.message_id, event.text)
                elif matching and isinstance(event, RefusalCompleted):
                    if refusal is None:
                        refusal = event
                    await append_message(event.message_id, event.text)
                elif matching and isinstance(event, ToolCallCompleted):
                    if event.provider_call_id not in {
                        item.provider_call_id for item in tool_calls
                    }:
                        tool_calls.append(event)
                elif matching and isinstance(event, StructuredOutputCompleted):
                    if not outputs:
                        outputs.append(event)
                elif matching and isinstance(event, UsageReported):
                    if usage is None:
                        usage = WaterbodyModelUsage(
                            input_tokens=event.input_tokens,
                            output_tokens=event.output_tokens,
                        )
                elif matching and isinstance(event, Terminal):
                    terminal = event
                    await write_response(
                        stored_finish_reason(event.finish_reason),
                        event.provider_response_id,
                    )
                yield event
        finally:
            if not response_written:
                await write_response(
                    (
                        StoredModelFinishReason.CANCELLED
                        if (
                            terminal is not None
                            and terminal.finish_reason is WaterbodyFinishReason.CANCELLED
                        )
                        or (
                            self._store._cancellation is not None
                            and self._store._cancellation.is_requested()
                        )
                        else StoredModelFinishReason.ERROR
                    ),
                    terminal.provider_response_id if terminal is not None else None,
                )
