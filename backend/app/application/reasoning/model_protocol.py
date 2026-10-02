import asyncio
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError

from app.application.reasoning.model_models import (
    RefusalCompleted,
    StructuredOutputCompleted,
    Terminal,
    ToolCallCompleted,
    UsageReported,
    WaterbodyFinishReason,
    WaterbodyModelCompletedResponse,
    WaterbodyModelContentEvent,
    WaterbodyModelErrorCode,
    WaterbodyModelPublicError,
    WaterbodyModelRequest,
    WaterbodyModelUsage,
    parse_waterbody_model_event,
)
from app.ports.errors import (
    WaterbodyModelCancelledError,
    WaterbodyModelContentFilteredError,
    WaterbodyModelIncompleteError,
    WaterbodyModelPortError,
    WaterbodyModelProtocolError,
    WaterbodyModelRefusalError,
    WaterbodyModelTruncatedError,
)
from app.ports.waterbody_model import WaterbodyModelPort


class CancellationProbe(Protocol):
    def is_requested(self) -> bool: ...


async def consume_waterbody_model_stream(
    port: WaterbodyModelPort,
    request: WaterbodyModelRequest,
    *,
    cancellation: CancellationProbe,
) -> WaterbodyModelCompletedResponse:
    if cancellation.is_requested():
        raise _port_error(WaterbodyModelCancelledError, request)

    content_events: list[WaterbodyModelContentEvent] = []
    usage: WaterbodyModelUsage | None = None
    terminal: Terminal | None = None
    structured_output_seen = False
    refusal_seen = False
    provider_call_ids: set[str] = set()

    try:
        async for raw_event in port.stream(request):
            if cancellation.is_requested():
                raise _port_error(WaterbodyModelCancelledError, request)
            try:
                event = parse_waterbody_model_event(raw_event)
            except (TypeError, ValueError, ValidationError):
                raise _port_error(WaterbodyModelProtocolError, request) from None

            if event.operation_id != request.operation_id or event.model != request.model:
                raise _port_error(WaterbodyModelProtocolError, request)
            if terminal is not None:
                raise _port_error(WaterbodyModelProtocolError, request)

            if isinstance(event, Terminal):
                terminal = event
            elif isinstance(event, UsageReported):
                if usage is not None:
                    raise _port_error(WaterbodyModelProtocolError, request)
                usage = WaterbodyModelUsage(
                    input_tokens=event.input_tokens,
                    output_tokens=event.output_tokens,
                )
            else:
                if isinstance(event, ToolCallCompleted):
                    if event.provider_call_id in provider_call_ids:
                        raise _port_error(WaterbodyModelProtocolError, request)
                    provider_call_ids.add(event.provider_call_id)
                elif isinstance(event, StructuredOutputCompleted):
                    if structured_output_seen:
                        raise _port_error(WaterbodyModelProtocolError, request)
                    structured_output_seen = True
                elif isinstance(event, RefusalCompleted):
                    if refusal_seen:
                        raise _port_error(WaterbodyModelProtocolError, request)
                    refusal_seen = True
                content_events.append(event)
    except WaterbodyModelPortError as error:
        if terminal is not None or not _error_matches_request(error, request):
            raise _port_error(WaterbodyModelProtocolError, request) from None
        raise
    except asyncio.CancelledError:
        raise _port_error(WaterbodyModelCancelledError, request) from None
    except Exception:
        raise _port_error(WaterbodyModelProtocolError, request) from None

    if cancellation.is_requested():
        raise _port_error(WaterbodyModelCancelledError, request)
    if terminal is None:
        raise _port_error(WaterbodyModelProtocolError, request)

    if terminal.finish_reason is WaterbodyFinishReason.COMPLETED:
        if refusal_seen or not content_events:
            raise _port_error(WaterbodyModelProtocolError, request)
        return WaterbodyModelCompletedResponse(
            operation_id=request.operation_id,
            phase=request.phase,
            model=request.model,
            provider_response_id=terminal.provider_response_id,
            events=tuple(content_events),
            usage=usage,
        )
    if terminal.finish_reason is WaterbodyFinishReason.CONTENT_FILTERED:
        raise _port_error(WaterbodyModelContentFilteredError, request)
    if terminal.finish_reason is WaterbodyFinishReason.REFUSED:
        if not refusal_seen:
            raise _port_error(WaterbodyModelProtocolError, request)
        raise _port_error(WaterbodyModelRefusalError, request)
    if terminal.finish_reason is WaterbodyFinishReason.TRUNCATED:
        raise _port_error(WaterbodyModelTruncatedError, request)
    if terminal.finish_reason is WaterbodyFinishReason.INCOMPLETE:
        raise _port_error(WaterbodyModelIncompleteError, request)
    raise _port_error(WaterbodyModelCancelledError, request)


def to_waterbody_model_public_error(
    error: WaterbodyModelPortError,
    request: WaterbodyModelRequest,
    *,
    error_id: UUID,
) -> WaterbodyModelPublicError:
    if not _error_matches_request(error, request):
        error = _port_error(WaterbodyModelProtocolError, request)
    return WaterbodyModelPublicError(
        error_id=error_id,
        provider=request.model.provider,
        model=request.model.model,
        code=WaterbodyModelErrorCode(error.code),
        failed_stage=request.phase,
        retryable=error.retryable,
    )


def _port_error(
    error_type: type[WaterbodyModelPortError],
    request: WaterbodyModelRequest,
) -> WaterbodyModelPortError:
    return error_type(
        provider=request.model.provider,
        model=request.model.model,
        operation_id=str(request.operation_id),
    )


def _error_matches_request(
    error: WaterbodyModelPortError,
    request: WaterbodyModelRequest,
) -> bool:
    return (
        error.provider == request.model.provider
        and error.model == request.model.model
        and error.operation == str(request.operation_id)
    )
