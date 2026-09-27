from __future__ import annotations

import json

from app.application.query_understanding import (
    ParserFinishReason,
    QueryParserInputV1,
    QueryParserResponse,
)
from app.application.query_understanding.parser_port import ParserDeltaHandler
from app.infrastructure.adapters._parsing import (
    invalid_response,
    require_list,
    require_mapping,
    require_string,
)
from app.infrastructure.http import ProviderHttpClient, ServerSentEvent
from app.ports import (
    InvalidPortResponseError,
    PortConfigurationError,
    PortUnavailableError,
)

PROVIDER = "openai"
_IGNORED_EVENT_TYPES = frozenset(
    {
        "response.queued",
        "response.created",
        "response.in_progress",
        "response.content_part.added",
        "response.content_part.done",
        "response.output_text.annotation.added",
        "response.reasoning_summary_part.added",
        "response.reasoning_summary_part.done",
        "response.reasoning_summary_text.delta",
        "response.reasoning_summary_text.done",
        "response.reasoning_text.delta",
        "response.reasoning_text.done",
    }
)


class OpenAIResponsesParserAdapter:
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
        self._http = http
        self._api_key = api_key
        self._endpoint = f"{base_url.rstrip('/')}/responses"

    async def generate(
        self,
        request: QueryParserInputV1,
        on_delta: ParserDeltaHandler,
    ) -> QueryParserResponse:
        if request.model.provider.casefold() != PROVIDER:
            raise PortConfigurationError(
                "The selected query parser provider is not configured.",
                provider=request.model.provider,
                operation="generate",
            )
        system, user = request.messages
        chunks: list[str] = []
        done_text: str | None = None
        saw_refusal = False
        terminal: tuple[dict[str, object], str] | None = None
        async with self._http.post_sse(
            self._endpoint,
            {
                "model": request.model.model,
                "instructions": system.text,
                "input": user.text,
                "text": {"format": {"type": "json_object"}},
                "tools": [],
                "store": False,
                "stream": True,
            },
            provider=PROVIDER,
            operation="generate",
            headers={
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        ) as events:
            async for event in events:
                root, event_type = self._event_payload(event)
                if terminal is not None:
                    raise invalid_response(PROVIDER, "generate")
                if event_type == "response.output_text.delta":
                    if done_text is not None:
                        raise invalid_response(PROVIDER, "generate")
                    delta = root.get("delta")
                    if not isinstance(delta, str) or not delta:
                        raise invalid_response(PROVIDER, "generate")
                    await on_delta(delta)
                    chunks.append(delta)
                    continue
                if event_type == "response.output_text.done":
                    text = root.get("text")
                    if done_text is not None or not isinstance(text, str):
                        raise invalid_response(PROVIDER, "generate")
                    done_text = text
                    continue
                if event_type in {"response.refusal.delta", "response.refusal.done"}:
                    saw_refusal = True
                    continue
                if event_type in {
                    "response.output_item.added",
                    "response.output_item.done",
                }:
                    item = require_mapping(
                        root.get("item"),
                        provider=PROVIDER,
                        operation="generate",
                    )
                    item_type = require_string(
                        item.get("type"),
                        provider=PROVIDER,
                        operation="generate",
                    )
                    if item_type not in {"message", "reasoning"}:
                        raise invalid_response(PROVIDER, "generate")
                    continue
                if event_type == "error":
                    raise self._unavailable()
                if event_type in {
                    "response.completed",
                    "response.incomplete",
                    "response.failed",
                    "response.cancelled",
                }:
                    terminal = (root, event_type)
                    continue
                if event_type not in _IGNORED_EVENT_TYPES:
                    raise invalid_response(PROVIDER, "generate")
        if terminal is None:
            raise invalid_response(PROVIDER, "generate")
        root, event_type = terminal
        return self._terminal_response(
            root,
            event_type=event_type,
            text="".join(chunks),
            done_text=done_text,
            saw_refusal=saw_refusal,
        )

    @staticmethod
    def _event_payload(
        event: ServerSentEvent,
    ) -> tuple[dict[str, object], str]:
        try:
            payload = json.loads(event.data)
        except json.JSONDecodeError as error:
            raise InvalidPortResponseError(
                "Provider returned an invalid event stream.",
                provider=PROVIDER,
                operation="generate",
            ) from error
        root = require_mapping(payload, provider=PROVIDER, operation="generate")
        event_type = require_string(
            root.get("type"),
            provider=PROVIDER,
            operation="generate",
        )
        if event.event not in {None, event_type}:
            raise invalid_response(PROVIDER, "generate")
        return dict(root), event_type

    @staticmethod
    def _terminal_response(
        root: dict[str, object],
        *,
        event_type: str,
        text: str,
        done_text: str | None,
        saw_refusal: bool,
    ) -> QueryParserResponse:
        response = require_mapping(
            root.get("response"),
            provider=PROVIDER,
            operation="generate",
        )
        status = require_string(
            response.get("status"),
            provider=PROVIDER,
            operation="generate",
        )
        expected_status = event_type.removeprefix("response.")
        if status != expected_status:
            raise invalid_response(PROVIDER, "generate")
        if status == "completed":
            completed_text, terminal_refusal = (
                OpenAIResponsesParserAdapter._completed_output(response)
            )
            if saw_refusal or terminal_refusal:
                finish_reason = ParserFinishReason.CONTENT_FILTER
            elif (
                text
                and done_text is not None
                and completed_text is not None
                and text == done_text == completed_text
            ):
                finish_reason = ParserFinishReason.STOP
            else:
                raise invalid_response(PROVIDER, "generate")
        elif status == "incomplete":
            details = require_mapping(
                response.get("incomplete_details"),
                provider=PROVIDER,
                operation="generate",
            )
            reason = details.get("reason")
            if not isinstance(reason, str):
                raise invalid_response(PROVIDER, "generate")
            if reason in {"max_output_tokens", "max_messages"}:
                finish_reason = ParserFinishReason.LENGTH
            elif reason == "content_filter":
                finish_reason = ParserFinishReason.CONTENT_FILTER
            else:
                raise invalid_response(PROVIDER, "generate")
        elif status in {"failed", "cancelled"}:
            raise OpenAIResponsesParserAdapter._unavailable()
        else:
            raise invalid_response(PROVIDER, "generate")
        return QueryParserResponse(text=text, finish_reason=finish_reason)

    @staticmethod
    def _completed_output(response: object) -> tuple[str | None, bool]:
        root = require_mapping(
            response,
            provider=PROVIDER,
            operation="generate",
        )
        output = require_list(
            root.get("output"),
            provider=PROVIDER,
            operation="generate",
        )
        texts: list[str] = []
        saw_refusal = False
        for raw_item in output:
            item = require_mapping(
                raw_item,
                provider=PROVIDER,
                operation="generate",
            )
            item_type = require_string(
                item.get("type"),
                provider=PROVIDER,
                operation="generate",
            )
            if item_type == "reasoning":
                continue
            if item_type != "message":
                raise invalid_response(PROVIDER, "generate")
            content = require_list(
                item.get("content"),
                provider=PROVIDER,
                operation="generate",
            )
            for raw_part in content:
                part = require_mapping(
                    raw_part,
                    provider=PROVIDER,
                    operation="generate",
                )
                part_type = require_string(
                    part.get("type"),
                    provider=PROVIDER,
                    operation="generate",
                )
                if part_type == "refusal":
                    if not isinstance(part.get("refusal"), str):
                        raise invalid_response(PROVIDER, "generate")
                    saw_refusal = True
                elif part_type == "output_text":
                    text = part.get("text")
                    if not isinstance(text, str):
                        raise invalid_response(PROVIDER, "generate")
                    texts.append(text)
                else:
                    raise invalid_response(PROVIDER, "generate")
        return ("".join(texts) if texts else None), saw_refusal

    @staticmethod
    def _unavailable() -> PortUnavailableError:
        return PortUnavailableError(
            "OpenAI did not complete the parser response.",
            provider=PROVIDER,
            operation="generate",
        )
