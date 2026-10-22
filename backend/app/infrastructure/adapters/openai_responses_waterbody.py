from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import NoReturn
from urllib.parse import urlsplit

from app.application.reasoning.model_models import (
    RefusalCompleted,
    StructuredOutputCompleted,
    Terminal,
    ToolCallCompleted,
    UsageReported,
    VisibleTextDelta,
    WaterbodyFinishReason,
    WaterbodyJsonSchema,
    WaterbodyMessageRole,
    WaterbodyModelEvent,
    WaterbodyModelPhase,
    WaterbodyModelRequest,
    WaterbodyToolDescriptor,
)
from app.application.run_summary.models import RunSummaryModelRequest
from app.infrastructure.adapters._parsing import (
    require_list,
    require_mapping,
    require_string,
)
from app.infrastructure.http import ProviderHttpClient, ServerSentEvent
from app.ports import (
    InvalidPortResponseError,
    PortConfigurationError,
    PortError,
    PortTimeoutError,
    WaterbodyModelCancelledError,
    WaterbodyModelPortError,
    WaterbodyModelProtocolError,
    WaterbodyModelTimeoutError,
    WaterbodyModelUnavailableError,
)

PROVIDER = "openai"
logger = logging.getLogger("wateragents.providers")
_TERMINAL_EVENTS = frozenset(
    {
        "response.completed",
        "response.incomplete",
        "response.failed",
        "response.cancelled",
    }
)
_REASONING_EVENTS = frozenset(
    {
        "response.reasoning_summary_part.added",
        "response.reasoning_summary_part.done",
        "response.reasoning_summary_text.delta",
        "response.reasoning_summary_text.done",
        "response.reasoning_text.delta",
        "response.reasoning_text.done",
    }
)
_BUILT_IN_TOOL_ITEM_TYPES = frozenset(
    {
        "computer_call",
        "custom_tool_call",
        "file_search_call",
        "mcp_call",
        "web_search_call",
    }
)
_DEEPSEEK_MAX_OUTPUT_TOKENS = 16_384
_DEEPSEEK_MAX_STREAM_BYTES = 16 * 1024 * 1024


class _JsonObjectPairs(list[tuple[str, object]]):
    pass


class _ConflictingDuplicateJsonKey(ValueError):
    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__("conflicting duplicate JSON key")


@dataclass(slots=True)
class _StreamState:
    response_id: str | None = None
    terminal_seen: bool = False
    added_items: dict[str, str] = field(default_factory=dict)
    completed_items: list[tuple[str, str]] = field(default_factory=list)
    text_deltas: dict[str, list[str]] = field(default_factory=dict)
    text_done: dict[str, str] = field(default_factory=dict)
    argument_deltas: dict[str, list[str]] = field(default_factory=dict)
    arguments_done: dict[str, str] = field(default_factory=dict)
    emitted_calls: dict[str, tuple[str, str]] = field(default_factory=dict)
    emitted_refusals: dict[str, str] = field(default_factory=dict)


class OpenAIResponsesWaterbodyAdapter:
    """Translate an OpenAI Responses SSE stream into the M6 model port."""

    def __init__(
        self,
        http: ProviderHttpClient,
        *,
        api_key: str,
        base_url: str,
    ) -> None:
        if not api_key:
            raise PortConfigurationError(
                "OpenAI API key is not configured.",
                provider=PROVIDER,
                operation="configure",
            )
        if not base_url:
            raise PortConfigurationError(
                "OpenAI base URL is not configured.",
                provider=PROVIDER,
                operation="configure",
            )
        self._http = http
        self._api_key = api_key
        self._endpoint = f"{base_url.rstrip('/')}/responses"
        parsed_base_url = urlsplit(base_url)
        self._deepseek_compat = (
            parsed_base_url.scheme == "https"
            and parsed_base_url.netloc.casefold() == "api.deepseek.com"
        )

    @property
    def max_output_tokens(self) -> int | None:
        return _DEEPSEEK_MAX_OUTPUT_TOKENS if self._deepseek_compat else None

    async def stream(
        self,
        request: WaterbodyModelRequest | RunSummaryModelRequest,
    ) -> AsyncIterator[WaterbodyModelEvent]:
        if request.model.provider.casefold() != PROVIDER:
            raise PortConfigurationError(
                "The selected waterbody model provider is not configured.",
                provider=request.model.provider,
                operation=str(request.operation_id),
            )

        operation = str(request.operation_id)
        descriptors = getattr(request, "tool_descriptors", ())
        tools = {descriptor.name: descriptor for descriptor in descriptors}
        state = _StreamState()
        event_type: str | None = None
        root: dict[str, object] | None = None
        try:
            async with self._http.post_sse(
                self._endpoint,
                _request_body(request, deepseek_compat=self._deepseek_compat),
                provider=PROVIDER,
                operation=operation,
                headers={
                    "Accept": "text/event-stream",
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                deadline=_monotonic_deadline(request.deadline),
                max_stream_bytes=(
                    _DEEPSEEK_MAX_STREAM_BYTES if self._deepseek_compat else None
                ),
            ) as events:
                async for event in events:
                    event_type = None
                    root = None
                    root, event_type = _event_payload(event, operation=operation)
                    if state.terminal_seen:
                        _protocol_error(request)

                    if event_type in {
                        "response.queued",
                        "response.created",
                        "response.in_progress",
                    }:
                        _capture_response_identity(root, request, state)
                        continue
                    if event_type == "response.output_item.added":
                        _record_added_item(root, request, state, tools)
                        continue
                    if event_type == "response.output_text.delta":
                        item_id = _root_item_id(root, request)
                        _require_item_type(state, item_id, "message", request)
                        delta = root.get("delta")
                        if not isinstance(delta, str) or not delta:
                            _protocol_error(request)
                        state.text_deltas.setdefault(item_id, []).append(delta)
                        yield VisibleTextDelta(
                            operation_id=request.operation_id,
                            model=request.model,
                            message_id=item_id,
                            text=delta,
                        )
                        continue
                    if event_type == "response.output_text.done":
                        item_id = _root_item_id(root, request)
                        _require_item_type(state, item_id, "message", request)
                        text = root.get("text")
                        if not isinstance(text, str) or item_id in state.text_done:
                            _protocol_error(request)
                        streamed = "".join(state.text_deltas.get(item_id, ()))
                        if streamed and streamed != text:
                            _protocol_error(request)
                        state.text_done[item_id] = text
                        continue
                    if event_type == "response.function_call_arguments.delta":
                        item_id = _root_item_id(root, request)
                        _require_item_type(state, item_id, "function_call", request)
                        delta = root.get("delta")
                        if not isinstance(delta, str) or not delta:
                            _protocol_error(request)
                        state.argument_deltas.setdefault(item_id, []).append(delta)
                        continue
                    if event_type == "response.function_call_arguments.done":
                        item_id = _root_item_id(root, request)
                        _require_item_type(state, item_id, "function_call", request)
                        arguments = root.get("arguments")
                        if (
                            not isinstance(arguments, str)
                            or not arguments.strip()
                            or item_id in state.arguments_done
                        ):
                            _protocol_error(request)
                        streamed = "".join(state.argument_deltas.get(item_id, ()))
                        if streamed and streamed != arguments:
                            _protocol_error(request)
                        state.arguments_done[item_id] = arguments
                        continue
                    if event_type == "response.refusal.done":
                        item_id = _root_item_id(root, request)
                        _require_item_type(state, item_id, "message", request)
                        refusal = root.get("refusal")
                        if (
                            not isinstance(refusal, str)
                            or not refusal.strip()
                            or item_id in state.emitted_refusals
                        ):
                            _protocol_error(request)
                        state.emitted_refusals[item_id] = refusal
                        yield RefusalCompleted(
                            operation_id=request.operation_id,
                            model=request.model,
                            message_id=item_id,
                            text=refusal,
                        )
                        continue
                    if event_type == "response.refusal.delta":
                        item_id = _root_item_id(root, request)
                        _require_item_type(state, item_id, "message", request)
                        if not isinstance(root.get("delta"), str):
                            _protocol_error(request)
                        continue
                    if event_type == "response.output_item.done":
                        translated = _record_completed_item(
                            root,
                            request,
                            state,
                            tools,
                            deepseek_compat=self._deepseek_compat,
                        )
                        if translated is not None:
                            yield translated
                        continue
                    if event_type in _REASONING_EVENTS:
                        continue
                    if event_type in {
                        "response.content_part.added",
                        "response.content_part.done",
                    }:
                        _validate_content_part_event(root, request, state)
                        continue
                    if event_type == "response.output_text.annotation.added":
                        item_id = _root_item_id(root, request)
                        _require_item_type(state, item_id, "message", request)
                        continue
                    if event_type == "error":
                        raise _typed_error(WaterbodyModelUnavailableError, request)
                    if event_type in _TERMINAL_EVENTS:
                        state.terminal_seen = True
                        async for translated in _terminal_events(
                            root,
                            event_type=event_type,
                            request=request,
                            state=state,
                            tools=tools,
                            deepseek_compat=self._deepseek_compat,
                        ):
                            yield translated
                        continue
                    _protocol_error(request)
        except asyncio.CancelledError:
            raise _typed_error(WaterbodyModelCancelledError, request) from None
        except WaterbodyModelProtocolError as error:
            _log_protocol_failure(operation, event_type, root, error)
            raise
        except WaterbodyModelUnavailableError:
            if event_type in {"error", "response.failed"}:
                _log_stream_unavailable(operation, event_type, root)
            raise
        except WaterbodyModelPortError:
            raise
        except PortTimeoutError:
            raise _typed_error(WaterbodyModelTimeoutError, request) from None
        except InvalidPortResponseError as error:
            _log_protocol_failure(operation, event_type, root, error)
            raise _typed_error(WaterbodyModelProtocolError, request) from None
        except PortError:
            raise _typed_error(WaterbodyModelUnavailableError, request) from None

        if not state.terminal_seen:
            _protocol_error(request)


def _request_body(
    request: WaterbodyModelRequest | RunSummaryModelRequest,
    *,
    deepseek_compat: bool = False,
) -> dict[str, object]:
    input_items: list[dict[str, object]] = []
    for message in request.messages:
        if message.role is WaterbodyMessageRole.TOOL:
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.provider_call_id,
                    "output": message.text,
                }
            )
        elif message.role is WaterbodyMessageRole.ASSISTANT and getattr(message, "provider_call_id", None) is not None:
            input_items.append({"type": "function_call", "call_id": message.provider_call_id,
                "name": message.tool_name, "arguments": message.text})
        else:
            input_items.append(
                {
                    "type": "message",
                    "role": message.role.value,
                    "content": message.text,
                }
            )
    instructions = request.system_prompt
    if deepseek_compat:
        instructions += "\nReturn any final text as one valid JSON object."
    body: dict[str, object] = {
        "model": request.model.model,
        "instructions": instructions,
        "input": input_items,
        "tools": [
            _tool_payload(item, deepseek_compat=deepseek_compat)
            for item in getattr(request, "tool_descriptors", ())
        ],
        "text": {
            "format": (
                {"type": "json_object"}
                if deepseek_compat
                else _schema_payload(request.response_schema)
            )
        },
        "store": False,
        "stream": True,
    }
    if deepseek_compat:
        # DeepSeek's default thinking mode can emit a long reasoning stream.
        # Bound both reasoning and visible output before the transport byte cap.
        body["reasoning"] = {"effort": "low"}
        body["max_output_tokens"] = _DEEPSEEK_MAX_OUTPUT_TOKENS
    return body


def _tool_payload(
    descriptor: WaterbodyToolDescriptor,
    *,
    deepseek_compat: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": "function",
        "name": descriptor.name,
        "description": descriptor.description,
        "parameters": json.loads(descriptor.input_schema.schema_text),
    }
    if not deepseek_compat:
        payload["strict"] = True
    return payload


def _schema_payload(schema: WaterbodyJsonSchema) -> dict[str, object]:
    return {
        "type": "json_schema",
        "name": schema.name,
        "schema": json.loads(schema.schema_text),
        "strict": True,
    }


def _monotonic_deadline(deadline: datetime) -> float:
    remaining = (deadline - datetime.now(UTC)).total_seconds()
    return asyncio.get_running_loop().time() + remaining


def _event_payload(
    event: ServerSentEvent,
    *,
    operation: str,
) -> tuple[dict[str, object], str]:
    try:
        payload = json.loads(event.data)
    except json.JSONDecodeError as error:
        raise InvalidPortResponseError(
            "Provider returned an invalid event stream.",
            provider=PROVIDER,
            operation=operation,
        ) from error
    root = require_mapping(payload, provider=PROVIDER, operation=operation)
    event_type = require_string(
        root.get("type"),
        provider=PROVIDER,
        operation=operation,
    )
    if event.event not in {None, event_type}:
        raise InvalidPortResponseError(
            "Provider returned an invalid event stream.",
            provider=PROVIDER,
            operation=operation,
        )
    return dict(root), event_type


def _capture_response_identity(
    root: Mapping[str, object],
    request: WaterbodyModelRequest,
    state: _StreamState,
) -> None:
    response = require_mapping(
        root.get("response"),
        provider=PROVIDER,
        operation=str(request.operation_id),
    )
    response_id = require_string(
        response.get("id"),
        provider=PROVIDER,
        operation=str(request.operation_id),
    )
    model = require_string(
        response.get("model"),
        provider=PROVIDER,
        operation=str(request.operation_id),
    )
    if model != request.model.model:
        _protocol_error(request)
    if state.response_id is not None and state.response_id != response_id:
        _protocol_error(request)
    state.response_id = response_id


def _record_added_item(
    root: Mapping[str, object],
    request: WaterbodyModelRequest,
    state: _StreamState,
    tools: Mapping[str, WaterbodyToolDescriptor],
) -> None:
    item = require_mapping(
        root.get("item"),
        provider=PROVIDER,
        operation=str(request.operation_id),
    )
    item_type = require_string(
        item.get("type"),
        provider=PROVIDER,
        operation=str(request.operation_id),
    )
    if item_type in _BUILT_IN_TOOL_ITEM_TYPES:
        _protocol_error(request)
    if item_type not in {"message", "function_call", "reasoning"}:
        _protocol_error(request)
    item_id = require_string(
        item.get("id"),
        provider=PROVIDER,
        operation=str(request.operation_id),
    )
    if item_id in state.added_items:
        _protocol_error(request)
    if item_type == "function_call":
        name = require_string(
            item.get("name"),
            provider=PROVIDER,
            operation=str(request.operation_id),
        )
        call_id = require_string(
            item.get("call_id"),
            provider=PROVIDER,
            operation=str(request.operation_id),
        )
        if name not in tools or call_id in state.emitted_calls:
            _protocol_error(request)
    state.added_items[item_id] = item_type


def _record_completed_item(
    root: Mapping[str, object],
    request: WaterbodyModelRequest,
    state: _StreamState,
    tools: Mapping[str, WaterbodyToolDescriptor],
    *,
    deepseek_compat: bool,
) -> ToolCallCompleted | None:
    item = require_mapping(
        root.get("item"),
        provider=PROVIDER,
        operation=str(request.operation_id),
    )
    item_type, item_id = _validate_completed_item(item, request, tools)
    _require_item_type(state, item_id, item_type, request)
    identity = (item_type, item_id)
    if identity in state.completed_items:
        _protocol_error(request)
    state.completed_items.append(identity)
    if item_type == "reasoning":
        return None
    if item_type == "message":
        _validate_message_against_stream(item, item_id, request, state)
        return None

    name = require_string(
        item.get("name"),
        provider=PROVIDER,
        operation=str(request.operation_id),
    )
    call_id = require_string(
        item.get("call_id"),
        provider=PROVIDER,
        operation=str(request.operation_id),
    )
    raw_arguments = item.get("arguments")
    if item_id in state.arguments_done and state.arguments_done[item_id] != raw_arguments:
        _protocol_error(request)
    arguments = _validated_arguments(
        raw_arguments, tools[name], request, deepseek_compat=deepseek_compat
    )
    if arguments != raw_arguments:
        logger.info(
            "provider_tool_argument_canonicalized",
            extra={
                "provider": request.model.provider,
                "provider_operation": str(request.operation_id),
                "tool_name": name,
                "field_name": "window_ids",
            },
        )
    previous = state.emitted_calls.get(call_id)
    if previous is not None and previous != (name, arguments):
        _protocol_error(request)
    if previous is not None:
        _protocol_error(request)
    state.emitted_calls[call_id] = (name, arguments)
    return _tool_call_event(
        request,
        call_id=call_id,
        name=name,
        arguments=arguments,
    )


def _validate_completed_item(
    item: Mapping[str, object],
    request: WaterbodyModelRequest,
    tools: Mapping[str, WaterbodyToolDescriptor],
) -> tuple[str, str]:
    item_type = require_string(
        item.get("type"),
        provider=PROVIDER,
        operation=str(request.operation_id),
    )
    if item_type in _BUILT_IN_TOOL_ITEM_TYPES:
        _protocol_error(request)
    if item_type not in {"message", "function_call", "reasoning"}:
        _protocol_error(request)
    item_id = require_string(
        item.get("id"),
        provider=PROVIDER,
        operation=str(request.operation_id),
    )
    if item_type == "function_call":
        name = require_string(
            item.get("name"),
            provider=PROVIDER,
            operation=str(request.operation_id),
        )
        if name not in tools:
            _protocol_error(request)
    return item_type, item_id


def _validate_message_against_stream(
    item: Mapping[str, object],
    item_id: str,
    request: WaterbodyModelRequest,
    state: _StreamState,
) -> None:
    text, refusal = _message_content(item, request)
    streamed = "".join(state.text_deltas.get(item_id, ()))
    done = state.text_done.get(item_id)
    if streamed and streamed != text:
        _protocol_error(request)
    if done is not None and done != text:
        _protocol_error(request)
    if refusal is not None:
        emitted = state.emitted_refusals.get(item_id)
        if emitted is not None and emitted != refusal:
            _protocol_error(request)


async def _terminal_events(
    root: Mapping[str, object],
    *,
    event_type: str,
    request: WaterbodyModelRequest,
    state: _StreamState,
    tools: Mapping[str, WaterbodyToolDescriptor],
    deepseek_compat: bool,
) -> AsyncIterator[WaterbodyModelEvent]:
    response = require_mapping(
        root.get("response"),
        provider=PROVIDER,
        operation=str(request.operation_id),
    )
    _capture_response_identity({"response": response}, request, state)
    status = require_string(
        response.get("status"),
        provider=PROVIDER,
        operation=str(request.operation_id),
    )
    if event_type != f"response.{status}":
        _protocol_error(request)
    if status == "failed":
        raise _typed_error(WaterbodyModelUnavailableError, request)

    output = require_list(
        response.get("output"),
        provider=PROVIDER,
        operation=str(request.operation_id),
    )
    terminal_items: list[tuple[str, str]] = []
    terminal_item_ids: set[str] = set()
    terminal_call_ids: set[str] = set()
    terminal_messages: list[tuple[str, str, str | None]] = []
    for raw_item in output:
        item = require_mapping(
            raw_item,
            provider=PROVIDER,
            operation=str(request.operation_id),
        )
        item_type, item_id = _validate_completed_item(item, request, tools)
        if item_id in terminal_item_ids:
            _protocol_error(request)
        terminal_item_ids.add(item_id)
        terminal_items.append((item_type, item_id))
        if item_type == "reasoning":
            continue
        if item_type == "function_call":
            name = require_string(
                item.get("name"),
                provider=PROVIDER,
                operation=str(request.operation_id),
            )
            call_id = require_string(
                item.get("call_id"),
                provider=PROVIDER,
                operation=str(request.operation_id),
            )
            if call_id in terminal_call_ids:
                _protocol_error(request)
            terminal_call_ids.add(call_id)
            arguments = _validated_arguments(
                item.get("arguments"),
                tools[name],
                request,
                deepseek_compat=deepseek_compat,
            )
            emitted = state.emitted_calls.get(call_id)
            if emitted is None:
                state.emitted_calls[call_id] = (name, arguments)
                yield _tool_call_event(
                    request,
                    call_id=call_id,
                    name=name,
                    arguments=arguments,
                )
            elif emitted != (name, arguments):
                _protocol_error(request)
            continue
        text, refusal = _message_content(item, request)
        _validate_message_against_stream(item, item_id, request, state)
        terminal_messages.append((item_id, text, refusal))

    if state.completed_items and state.completed_items != terminal_items:
        _protocol_error(request)

    refusal_messages = tuple(item for item in terminal_messages if item[2] is not None)
    if refusal_messages:
        if len(refusal_messages) != 1 or any(
            text for _, text, _ in terminal_messages
        ) or state.emitted_calls:
            _protocol_error(request)
        message_id, _, refusal = refusal_messages[0]
        if refusal is None:
            _protocol_error(request)
        emitted = state.emitted_refusals.get(message_id)
        if emitted is None:
            state.emitted_refusals[message_id] = refusal
            yield RefusalCompleted(
                operation_id=request.operation_id,
                model=request.model,
                message_id=message_id,
                text=refusal,
            )
        elif emitted != refusal:
            _protocol_error(request)

    if status == "completed":
        if refusal_messages:
            finish_reason = WaterbodyFinishReason.REFUSED
        else:
            text_messages = tuple(item for item in terminal_messages if item[1].strip())
            if text_messages:
                output_id, value, _ = text_messages[-1]
                if deepseek_compat:
                    try:
                        value, duplicate_paths = _canonicalize_duplicate_json_keys(value)
                    except _ConflictingDuplicateJsonKey as error:
                        logger.warning(
                            "provider_output_duplicate_key_conflict",
                            extra={
                                "provider": request.model.provider,
                                "provider_operation": str(request.operation_id),
                                "duplicate_path": error.path,
                            },
                        )
                        _protocol_error(request)
                    if duplicate_paths:
                        logger.info(
                            "provider_output_duplicate_keys_canonicalized",
                            extra={
                                "provider": request.model.provider,
                                "provider_operation": str(request.operation_id),
                                "duplicate_key_count": len(duplicate_paths),
                                "duplicate_key_paths": list(dict.fromkeys(duplicate_paths))[:8],
                            },
                        )
                if _validate_schema_output(value, request.response_schema, request):
                    yield _structured_output_event(request, output_id=output_id, value=value)
                elif not state.text_deltas.get(output_id):
                    yield VisibleTextDelta(operation_id=request.operation_id, model=request.model,
                                           message_id=output_id, text=value)
            if not text_messages and not state.emitted_calls:
                _protocol_error(request)
            finish_reason = WaterbodyFinishReason.COMPLETED
    elif status == "cancelled":
        finish_reason = WaterbodyFinishReason.CANCELLED
    elif status == "incomplete":
        finish_reason = _incomplete_reason(response, request)
    else:
        _protocol_error(request)

    usage = response.get("usage")
    if usage is not None:
        input_tokens, output_tokens = _usage_counts(usage, request)
        yield UsageReported(
            operation_id=request.operation_id,
            model=request.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    if state.response_id is None:
        _protocol_error(request)
    yield Terminal(
        operation_id=request.operation_id,
        model=request.model,
        provider_response_id=state.response_id,
        finish_reason=finish_reason,
    )


def _message_content(
    item: Mapping[str, object],
    request: WaterbodyModelRequest,
) -> tuple[str, str | None]:
    content = require_list(
        item.get("content"),
        provider=PROVIDER,
        operation=str(request.operation_id),
    )
    texts: list[str] = []
    refusals: list[str] = []
    for raw_part in content:
        part = require_mapping(
            raw_part,
            provider=PROVIDER,
            operation=str(request.operation_id),
        )
        part_type = require_string(
            part.get("type"),
            provider=PROVIDER,
            operation=str(request.operation_id),
        )
        if part_type == "output_text":
            text = part.get("text")
            if not isinstance(text, str):
                _protocol_error(request)
            texts.append(text)
        elif part_type == "refusal":
            refusal = part.get("refusal")
            if not isinstance(refusal, str) or not refusal.strip():
                _protocol_error(request)
            refusals.append(refusal)
        else:
            _protocol_error(request)
    if refusals and (texts or len(refusals) != 1):
        _protocol_error(request)
    return "".join(texts), refusals[0] if refusals else None


def _validate_content_part_event(
    root: Mapping[str, object],
    request: WaterbodyModelRequest,
    state: _StreamState,
) -> None:
    item_id = _root_item_id(root, request)
    item_type = state.added_items.get(item_id)
    if item_type not in {"message", "reasoning"}:
        _protocol_error(request)
    part = require_mapping(
        root.get("part"),
        provider=PROVIDER,
        operation=str(request.operation_id),
    )
    part_type = require_string(
        part.get("type"),
        provider=PROVIDER,
        operation=str(request.operation_id),
    )
    if (item_type, part_type) not in {
        ("message", "output_text"),
        ("message", "refusal"),
        ("reasoning", "reasoning_text"),
    }:
        _protocol_error(request)


def _validated_arguments(
    raw: object,
    descriptor: WaterbodyToolDescriptor,
    request: WaterbodyModelRequest,
    *,
    deepseek_compat: bool,
) -> str:
    if not isinstance(raw, str) or not raw.strip():
        _protocol_error(request)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        _protocol_error(request)
    if not isinstance(value, dict):
        _protocol_error(request)
    if deepseek_compat and "window_ids" not in value:
        schema = json.loads(descriptor.input_schema.schema_text)
        window_ids = (
            schema.get("properties", {})
            .get("window_ids", {})
            .get("items", {})
            .get("enum", ())
        )
        if isinstance(window_ids, list) and len(window_ids) == 1:
            value["window_ids"] = [window_ids[0]]
            raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    _validate_json_schema(
        value, descriptor.input_schema, request, tool_name=descriptor.name
    )
    return raw


def _tool_call_event(
    request: WaterbodyModelRequest,
    *,
    call_id: str,
    name: str,
    arguments: str,
) -> ToolCallCompleted:
    try:
        return ToolCallCompleted(
            operation_id=request.operation_id,
            model=request.model,
            provider_call_id=call_id,
            name=name,
            arguments=arguments,
        )
    except (TypeError, ValueError):
        _protocol_error(request)


def _structured_output_event(
    request: WaterbodyModelRequest,
    *,
    output_id: str,
    value: str,
) -> StructuredOutputCompleted:
    try:
        return StructuredOutputCompleted(
            operation_id=request.operation_id,
            model=request.model,
            output_id=output_id,
            value=value,
        )
    except (TypeError, ValueError):
        _protocol_error(request)


def _validate_schema_output(
    raw: str,
    schema: WaterbodyJsonSchema,
    request: WaterbodyModelRequest | RunSummaryModelRequest,
) -> bool:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        # Keep completed object text visible for the application's single
        # repair without accepting it as a structured result.
        if (isinstance(request, WaterbodyModelRequest)
                and request.phase in {WaterbodyModelPhase.REASONING_BATCH, WaterbodyModelPhase.OVERALL_SYNTHESIS}
                and raw.lstrip().startswith("{")):
            return False
        _protocol_error(request)
    if not isinstance(value, dict):
        _protocol_error(request)
    if (
        isinstance(request, WaterbodyModelRequest)
        and request.phase is WaterbodyModelPhase.EVIDENCE_PLANNING
    ):
        _validate_json_schema(value, schema, request)
    return True


def _canonicalize_duplicate_json_keys(raw: str) -> tuple[str, tuple[str, ...]]:
    try:
        parsed = json.loads(raw, object_pairs_hook=_JsonObjectPairs)
    except json.JSONDecodeError:
        return raw, ()

    duplicate_paths: list[str] = []

    def normalize(value: object, path: str) -> object:
        if isinstance(value, _JsonObjectPairs):
            result: dict[str, object] = {}
            for key, item in value:
                field = (
                    f".{key}"
                    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", key)
                    else ".[field]"
                )
                item_path = f"{path}{field}"
                normalized = normalize(item, item_path)
                if key in result:
                    if json.dumps(result[key], sort_keys=True) != json.dumps(
                        normalized, sort_keys=True
                    ):
                        raise _ConflictingDuplicateJsonKey(item_path)
                    duplicate_paths.append(item_path)
                else:
                    result[key] = normalized
            return result
        if isinstance(value, list):
            return [normalize(item, f"{path}[{index}]") for index, item in enumerate(value)]
        return value

    normalized = normalize(parsed, "$")
    if not duplicate_paths:
        return raw, ()
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":")), tuple(
        duplicate_paths
    )


def _validate_json_schema(
    value: object,
    schema: WaterbodyJsonSchema,
    request: WaterbodyModelRequest,
    *,
    tool_name: str | None = None,
) -> None:
    try:
        root = json.loads(schema.schema_text)
        if not isinstance(root, dict):
            raise _SchemaMismatch("$", "schema_root")
        _check_schema(value, root, root)
    except _SchemaMismatch as error:
        logger.warning(
            "provider_schema_mismatch",
            extra={
                "provider": request.model.provider,
                "provider_operation": str(request.operation_id),
                "schema_name": schema.name,
                "tool_name": tool_name,
                "schema_path": error.path,
                "schema_rule": error.rule,
            },
        )
        _protocol_error(request)
    except json.JSONDecodeError:
        _protocol_error(request)


class _SchemaMismatch(Exception):
    def __init__(self, path: str = "$", rule: str = "schema") -> None:
        self.path = path
        self.rule = rule
        super().__init__(f"{path}:{rule}")


def _check_schema(
    value: object,
    schema: object,
    root: Mapping[str, object],
    path: str = "$",
) -> None:
    if not isinstance(schema, dict):
        raise _SchemaMismatch(path, "schema")
    if "$ref" in schema:
        reference = schema["$ref"]
        if not isinstance(reference, str) or not reference.startswith("#/"):
            raise _SchemaMismatch(path, "ref")
        target: object = root
        for token in reference[2:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or token not in target:
                raise _SchemaMismatch(path, "ref")
            target = target[token]
        _check_schema(value, target, root, path)
        return
    if "oneOf" in schema:
        choices = schema["oneOf"]
        if not isinstance(choices, list):
            raise _SchemaMismatch(path, "oneOf_schema")
        matches = sum(_schema_matches(value, choice, root) for choice in choices)
        if matches != 1:
            raise _SchemaMismatch(path, "oneOf")
    if "anyOf" in schema:
        choices = schema["anyOf"]
        if not isinstance(choices, list) or not any(
            _schema_matches(value, choice, root) for choice in choices
        ):
            raise _SchemaMismatch(path, "anyOf")
    if "const" in schema and value != schema["const"]:
        raise _SchemaMismatch(path, "const")
    if "enum" in schema:
        choices = schema["enum"]
        if not isinstance(choices, list) or value not in choices:
            raise _SchemaMismatch(path, "enum")

    expected_type = schema.get("type")
    if expected_type is not None and not _has_json_type(value, expected_type):
        raise _SchemaMismatch(path, "type")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise _SchemaMismatch(path, "object_schema")
        for key in required:
            if not isinstance(key, str):
                raise _SchemaMismatch(path, "object_schema")
            if key not in value:
                raise _SchemaMismatch(f"{path}.{key}", "required")
        if schema.get("additionalProperties") is False and not set(value) <= set(properties):
            raise _SchemaMismatch(path, "additionalProperties")
        for key, item in value.items():
            if key in properties:
                _check_schema(item, properties[key], root, f"{path}.{key}")
    if isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            raise _SchemaMismatch(path, "minItems")
        if isinstance(maximum, int) and len(value) > maximum:
            raise _SchemaMismatch(path, "maxItems")
        if schema.get("uniqueItems") is True:
            canonical = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in value]
            if len(canonical) != len(set(canonical)):
                raise _SchemaMismatch(path, "uniqueItems")
        if "items" in schema:
            for index, item in enumerate(value):
                _check_schema(item, schema["items"], root, f"{path}[{index}]")
    if isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        pattern = schema.get("pattern")
        if isinstance(minimum, int) and len(value) < minimum:
            raise _SchemaMismatch(path, "minLength")
        if isinstance(maximum, int) and len(value) > maximum:
            raise _SchemaMismatch(path, "maxLength")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            raise _SchemaMismatch(path, "pattern")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(value, float) and not math.isfinite(value):
            raise _SchemaMismatch(path, "finite")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise _SchemaMismatch(path, "minimum")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise _SchemaMismatch(path, "maximum")


def _schema_matches(value: object, schema: object, root: Mapping[str, object]) -> bool:
    try:
        _check_schema(value, schema, root)
    except _SchemaMismatch:
        return False
    return True


def _has_json_type(value: object, expected: object) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise _SchemaMismatch


def _usage_counts(
    raw: object,
    request: WaterbodyModelRequest,
) -> tuple[int | None, int | None]:
    usage = require_mapping(
        raw,
        provider=PROVIDER,
        operation=str(request.operation_id),
    )
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    for value in (input_tokens, output_tokens):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            _protocol_error(request)
    if input_tokens is None and output_tokens is None:
        _protocol_error(request)
    return input_tokens, output_tokens


def _incomplete_reason(
    response: Mapping[str, object],
    request: WaterbodyModelRequest,
) -> WaterbodyFinishReason:
    details = require_mapping(
        response.get("incomplete_details"),
        provider=PROVIDER,
        operation=str(request.operation_id),
    )
    reason = require_string(
        details.get("reason"),
        provider=PROVIDER,
        operation=str(request.operation_id),
    )
    if reason in {"max_output_tokens", "max_messages"}:
        return WaterbodyFinishReason.TRUNCATED
    if reason == "content_filter":
        return WaterbodyFinishReason.CONTENT_FILTERED
    if reason in {"tool_error", "server_error"}:
        return WaterbodyFinishReason.INCOMPLETE
    _protocol_error(request)


def _root_item_id(
    root: Mapping[str, object],
    request: WaterbodyModelRequest,
) -> str:
    return require_string(
        root.get("item_id"),
        provider=PROVIDER,
        operation=str(request.operation_id),
    )


def _require_item_type(
    state: _StreamState,
    item_id: str,
    expected: str,
    request: WaterbodyModelRequest,
) -> None:
    if state.added_items.get(item_id) != expected:
        _protocol_error(request)


def _typed_error(
    error_type: type[WaterbodyModelPortError],
    request: WaterbodyModelRequest,
) -> WaterbodyModelPortError:
    return error_type(
        provider=request.model.provider,
        model=request.model.model,
        operation_id=str(request.operation_id),
    )


def _protocol_error(request: WaterbodyModelRequest) -> NoReturn:
    raise _typed_error(WaterbodyModelProtocolError, request)


def _log_protocol_failure(
    operation: str,
    event_type: str | None,
    root: Mapping[str, object] | None,
    error: BaseException,
) -> None:
    traceback = error.__traceback__
    caller = None
    while traceback is not None:
        if traceback.tb_frame.f_code.co_name == "_protocol_error":
            break
        caller = traceback
        traceback = traceback.tb_next
    item = root.get("item") if root is not None else None
    response = root.get("response") if root is not None else None
    logger.warning(
        "provider_stream_protocol_invalid",
        extra={
            "provider": PROVIDER,
            "provider_operation": operation,
            "stream_event_type": event_type,
            "stream_item_type": item.get("type") if isinstance(item, dict) else None,
            "stream_response_status": (
                response.get("status") if isinstance(response, dict) else None
            ),
            "stream_payload_keys": sorted(root) if root is not None else (),
            "stream_failure_rule": (
                f"{caller.tb_frame.f_code.co_name}:{caller.tb_lineno}"
                if caller is not None else type(error).__name__
            ),
        },
    )


def _log_stream_unavailable(
    operation: str,
    event_type: str,
    root: Mapping[str, object] | None,
) -> None:
    response = root.get("response") if root is not None else None
    details = root.get("error") if root is not None else None
    if not isinstance(details, dict) and isinstance(response, dict):
        details = response.get("error")
    fields = {}
    if isinstance(details, dict):
        fields = {
            name: value
            for name, value in (
                ("stream_error_type", details.get("type")),
                ("stream_error_code", details.get("code")),
            )
            if isinstance(value, str) and 0 < len(value) <= 128
        }
    logger.warning(
        "provider_stream_failed",
        extra={
            "provider": PROVIDER,
            "provider_operation": operation,
            "stream_event_type": event_type,
            **fields,
        },
    )
