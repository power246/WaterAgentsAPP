from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import ValidationError

from app.application.analysis_runs.models import PublicError
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
from app.application.run_summary.errors import RunSummaryValidationError
from app.application.run_summary.models import (
    RunSummaryInput,
    RunSummaryModelRequest,
    RunSummaryResult,
)
from app.application.run_summary.prompt_builder import RunSummaryPromptBuilder
from app.application.run_summary.validator import RunSummaryValidator
from app.application.transcripts.models import TranscriptContentRef
from app.domain import ModelSelection, ModelUsageSummary
from app.ports.errors import (
    WaterbodyModelCancelledError,
    WaterbodyModelContentFilteredError,
    WaterbodyModelIncompleteError,
    WaterbodyModelPortError,
    WaterbodyModelProtocolError,
    WaterbodyModelRefusalError,
    WaterbodyModelTruncatedError,
)


logger = logging.getLogger("wateragents.run_summary")


class CancellationProbe(Protocol):
    def is_requested(self) -> bool: ...


class RunSummaryModelPort(Protocol):
    async def stream(
        self,
        request: RunSummaryModelRequest,
    ) -> AsyncIterator[WaterbodyModelEvent]: ...


class _NeverCancelled:
    def is_requested(self) -> bool:
        return False


class RunSummaryRunStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class RunSummaryRunOutcome:
    status: RunSummaryRunStatus
    result: RunSummaryResult | None
    error: PublicError | None
    model_calls: int
    repaired: bool

    def __post_init__(self) -> None:
        if (self.status is RunSummaryRunStatus.COMPLETED) != (self.result is not None):
            raise ValueError("completed summary outcome requires one result")
        if (self.status is RunSummaryRunStatus.FAILED) != (self.error is not None):
            raise ValueError("failed summary outcome requires one public error")
        if self.model_calls not in {0, 1, 2}:
            raise ValueError("summary runner permits at most two model calls")
        if self.repaired != (self.model_calls == 2):
            raise ValueError("summary repair state must match model call count")


@dataclass(frozen=True, slots=True)
class _CompletedCall:
    structured_output: str
    visible_text: str
    usage: ModelUsageSummary | None


class _Clock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class RunSummaryTranscript(Protocol):
    async def write_input(self, value: RunSummaryInput) -> TranscriptContentRef: ...
    def recording_port(self, delegate: RunSummaryModelPort) -> RunSummaryModelPort: ...
    async def write_result(self, value: RunSummaryResult) -> TranscriptContentRef: ...
    async def fail(self, error: PublicError): ...
    async def cancel(self): ...


class RunSummaryRunner:
    """One summary call, with at most one same-model schema/fact repair."""

    def __init__(
        self,
        *,
        model_port: RunSummaryModelPort,
        prompt_builder: RunSummaryPromptBuilder | None = None,
        validator: RunSummaryValidator | None = None,
        transcript_store: RunSummaryTranscript | None = None,
        operation_id_factory: Callable[[], UUID] = uuid4,
        error_id_factory: Callable[[], UUID] = uuid4,
        clock: object | None = None,
    ) -> None:
        self._model_port = model_port
        self._prompt_builder = prompt_builder or RunSummaryPromptBuilder()
        self._validator = validator or RunSummaryValidator()
        self._transcript = transcript_store
        self._operation_id_factory = operation_id_factory
        self._error_id_factory = error_id_factory
        self._clock = clock or _Clock()

    async def run(
        self,
        summary_input: RunSummaryInput,
        *,
        model: ModelSelection,
        deadline: datetime,
        input_ref: TranscriptContentRef | None = None,
        cancellation: CancellationProbe | None = None,
    ) -> RunSummaryRunOutcome:
        cancellation = cancellation or _NeverCancelled()
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("summary deadline must be timezone-aware")
        target_model = summary_input.target_result_projection.model_execution_summary.model
        if target_model != model:
            raise ValueError("summary model must exactly match the run waterbody model")
        if self._transcript is not None:
            stored_ref = await self._transcript.write_input(summary_input)
            if input_ref is not None and input_ref != stored_ref:
                raise ValueError("summary input reference changed while being stored")
            input_ref = stored_ref
            port = self._transcript.recording_port(self._model_port)
        else:
            port = self._model_port
        if input_ref is None:
            raise ValueError("summary runner requires its stored input reference")
        if input_ref.run_id != summary_input.run_id:
            raise ValueError("summary input reference must remain in the run")

        request = self._prompt_builder.build(
            summary_input,
            model=model,
            input_ref=input_ref,
            operation_id=self._operation_id_factory(),
            deadline=deadline,
        )
        calls = 0
        usage: list[ModelUsageSummary] = []
        try:
            calls += 1
            completed = await _consume(port, request, cancellation=cancellation)
            if completed.usage is not None:
                usage.append(completed.usage)
            try:
                _, result = self._validator.validate(
                    summary_input,
                    completed.structured_output,
                    model=model,
                    usage=_combined_usage(usage),
                    generated_at=self._clock.now(),
                )
            except RunSummaryValidationError as initial_error:
                repair = self._prompt_builder.build_repair(
                    summary_input,
                    original=request,
                    operation_id=uuid5(
                        NAMESPACE_URL,
                        f"run-summary-repair:{request.operation_id}",
                    ),
                    original_output=(
                        completed.structured_output or completed.visible_text
                    ),
                    issues=initial_error.issues,
                )
                calls += 1
                repaired = await _consume(port, repair, cancellation=cancellation)
                if repaired.usage is not None:
                    usage.append(repaired.usage)
                _, result = self._validator.validate(
                    summary_input,
                    repaired.structured_output,
                    model=model,
                    usage=_combined_usage(usage),
                    generated_at=self._clock.now(),
                )
        except WaterbodyModelCancelledError:
            if self._transcript is not None:
                await self._transcript.cancel()
            return RunSummaryRunOutcome(
                status=RunSummaryRunStatus.CANCELLED,
                result=None,
                error=None,
                model_calls=calls,
                repaired=calls == 2,
            )
        except (RunSummaryValidationError, WaterbodyModelPortError) as error:
            logger.warning(
                "run_summary_model_failed",
                extra={
                    "run_id": str(summary_input.run_id),
                    "session_id": str(summary_input.session_id),
                    "failure_code": getattr(error, "code", "run_summary_invalid"),
                    "model_calls": calls,
                },
            )
            public_error = _public_error(error, self._error_id_factory())
            if self._transcript is not None:
                await self._transcript.fail(public_error)
            return RunSummaryRunOutcome(
                status=RunSummaryRunStatus.FAILED,
                result=None,
                error=public_error,
                model_calls=calls,
                repaired=calls == 2,
            )

        if self._transcript is not None:
            await self._transcript.write_result(result)
        return RunSummaryRunOutcome(
            status=RunSummaryRunStatus.COMPLETED,
            result=result,
            error=None,
            model_calls=calls,
            repaired=calls == 2,
        )


async def _consume(
    port: RunSummaryModelPort,
    request: RunSummaryModelRequest,
    *,
    cancellation: CancellationProbe,
) -> _CompletedCall:
    if cancellation.is_requested():
        raise _port_error(WaterbodyModelCancelledError, request)
    structured: StructuredOutputCompleted | None = None
    usage: ModelUsageSummary | None = None
    terminal: Terminal | None = None
    visible: list[str] = []
    refusal = False
    try:
        async for raw in port.stream(request):
            if cancellation.is_requested():
                raise _port_error(WaterbodyModelCancelledError, request)
            try:
                event = parse_waterbody_model_event(raw)
            except (TypeError, ValueError, ValidationError):
                raise _port_error(WaterbodyModelProtocolError, request) from None
            if (
                event.operation_id != request.operation_id
                or event.model != request.model
                or terminal is not None
            ):
                raise _port_error(WaterbodyModelProtocolError, request)
            if isinstance(event, ToolCallCompleted):
                raise _port_error(WaterbodyModelProtocolError, request)
            if isinstance(event, VisibleTextDelta):
                visible.append(event.text)
            elif isinstance(event, StructuredOutputCompleted):
                if structured is not None:
                    raise _port_error(WaterbodyModelProtocolError, request)
                structured = event
            elif isinstance(event, RefusalCompleted):
                refusal = True
            elif isinstance(event, UsageReported):
                if usage is not None:
                    raise _port_error(WaterbodyModelProtocolError, request)
                usage = ModelUsageSummary(
                    input_tokens=event.input_tokens,
                    output_tokens=event.output_tokens,
                )
            elif isinstance(event, Terminal):
                terminal = event
    except WaterbodyModelPortError as error:
        if (
            error.provider != request.model.provider
            or error.model != request.model.model
            or error.operation != str(request.operation_id)
        ):
            raise _port_error(WaterbodyModelProtocolError, request) from None
        raise
    except Exception:
        raise _port_error(WaterbodyModelProtocolError, request) from None

    if cancellation.is_requested():
        raise _port_error(WaterbodyModelCancelledError, request)
    if terminal is None:
        raise _port_error(WaterbodyModelProtocolError, request)
    error_type = {
        WaterbodyFinishReason.CANCELLED: WaterbodyModelCancelledError,
        WaterbodyFinishReason.CONTENT_FILTERED: WaterbodyModelContentFilteredError,
        WaterbodyFinishReason.INCOMPLETE: WaterbodyModelIncompleteError,
        WaterbodyFinishReason.REFUSED: WaterbodyModelRefusalError,
        WaterbodyFinishReason.TRUNCATED: WaterbodyModelTruncatedError,
    }.get(terminal.finish_reason)
    if error_type is not None:
        raise _port_error(error_type, request)
    if refusal:
        raise _port_error(WaterbodyModelProtocolError, request)
    return _CompletedCall(
        structured_output=(
            structured.value if structured is not None else "".join(visible)
        ),
        visible_text="".join(visible),
        usage=usage,
    )


def _combined_usage(values: list[ModelUsageSummary]) -> ModelUsageSummary | None:
    if not values:
        return None
    inputs = [value.input_tokens for value in values if value.input_tokens is not None]
    outputs = [value.output_tokens for value in values if value.output_tokens is not None]
    return ModelUsageSummary(
        input_tokens=sum(inputs) if inputs else None,
        output_tokens=sum(outputs) if outputs else None,
    )


def _port_error(
    error_type: type[WaterbodyModelPortError],
    request: RunSummaryModelRequest,
) -> WaterbodyModelPortError:
    return error_type(
        provider=request.model.provider,
        model=request.model.model,
        operation_id=str(request.operation_id),
    )


def _public_error(error: Exception, error_id: UUID) -> PublicError:
    if isinstance(error, RunSummaryValidationError):
        return PublicError(
            error_id=error_id,
            code="run_summary_invalid",
            message="The run summary could not be validated.",
            failed_stage="run_summarization",
            retryable=False,
        )
    return PublicError(
        error_id=error_id,
        code="run_summary_failed",
        message="The run summary could not be generated.",
        failed_stage="run_summarization",
        retryable=not isinstance(
            error,
            (WaterbodyModelProtocolError, WaterbodyModelRefusalError),
        ),
    )
