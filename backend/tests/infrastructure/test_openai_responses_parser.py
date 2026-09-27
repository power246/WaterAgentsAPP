import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterable

import httpx
import pytest

from app.application.query_understanding import (
    ParserFinishReason,
    ParserMessageRole,
    ParserPromptMessage,
    QueryParserInputV1,
)
from app.domain import ModelSelection
from app.infrastructure.adapters import OpenAIResponsesParserAdapter
from app.infrastructure.http import ProviderHttpClient
from app.ports import (
    InvalidPortResponseError,
    PortConfigurationError,
    PortHttpError,
    PortResponseLimitError,
    PortTimeoutError,
    PortUnavailableError,
)


def run(coroutine):
    return asyncio.run(coroutine)


def parser_request(*, provider: str = "openai") -> QueryParserInputV1:
    return QueryParserInputV1(
        model=ModelSelection(provider=provider, model="gpt-parser"),
        messages=(
            ParserPromptMessage(
                role=ParserMessageRole.SYSTEM,
                text="Return one JSON object.",
            ),
            ParserPromptMessage(
                role=ParserMessageRole.USER,
                text='{"query":"river"}',
            ),
        ),
    )


def client(
    handler,
    *,
    timeout_seconds: float = 1,
    max_response_bytes: int = 100_000,
) -> ProviderHttpClient:
    return ProviderHttpClient(
        timeout_seconds=timeout_seconds,
        max_response_bytes=max_response_bytes,
        retry_delay_seconds=0,
        transport=httpx.MockTransport(handler),
    )


def sse_event(event_type: str, **fields: object) -> bytes:
    payload = json.dumps(
        {"type": event_type, **fields},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"event: {event_type}\r\ndata: {payload}\r\n\r\n".encode()


def terminal_event(status: str, **response_fields: object) -> bytes:
    return sse_event(
        f"response.{status}",
        response={"status": status, **response_fields},
    )


def output_text_done(text: object) -> bytes:
    return sse_event("response.output_text.done", text=text)


def completed_terminal(
    text: object,
    *,
    leading_output: Iterable[object] = (),
) -> bytes:
    return terminal_event(
        "completed",
        output=[
            *leading_output,
            {
                "type": "message",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
    )


class _StaticStream(httpx.AsyncByteStream):
    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks = tuple(chunks)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


class GatedStream(httpx.AsyncByteStream):
    def __init__(self, before: Iterable[bytes], after: Iterable[bytes]) -> None:
        self._before = tuple(before)
        self._after = tuple(after)
        self.release = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._before:
            yield chunk
        await self.release.wait()
        for chunk in self._after:
            yield chunk

    async def aclose(self) -> None:
        self.closed.set()


def response_for(request: httpx.Request, chunks: Iterable[bytes]) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream; charset=utf-8"},
        stream=_StaticStream(chunks),
        request=request,
    )


def test_completed_reasoning_and_message_stream_deltas_before_terminal() -> None:
    requests: list[httpx.Request] = []
    first = sse_event(
        "response.output_text.delta",
        delta='{"location_mention":"',
    )
    second = sse_event("response.output_text.delta", delta='河流"}')
    unicode_split = second.index("河".encode()) + 1
    stream = GatedStream(
        (
            sse_event(
                "response.output_item.added",
                item={"type": "reasoning"},
            ),
            sse_event("response.reasoning_text.delta", delta="not public"),
            sse_event(
                "response.output_item.done",
                item={"type": "reasoning"},
            ),
            sse_event(
                "response.output_item.added",
                item={"type": "message"},
            ),
            first[:9],
            first[9:31],
            first[31:],
        ),
        (
            second[:unicode_split],
            second[unicode_split:],
            output_text_done('{"location_mention":"河流"}'),
            sse_event(
                "response.output_item.done",
                item={"type": "message"},
            ),
            completed_terminal(
                '{"location_mention":"河流"}',
                leading_output=({"type": "reasoning", "summary": []},),
            ),
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream; charset=utf-8"},
            stream=stream,
            request=request,
        )

    async def exercise() -> None:
        deltas: list[str] = []
        first_delta = asyncio.Event()

        async def collect(text: str) -> None:
            deltas.append(text)
            first_delta.set()

        async with client(handler) as http:
            task = asyncio.create_task(
                OpenAIResponsesParserAdapter(
                    http,
                    api_key="openai-secret",
                    base_url="https://api.openai.test/v1",
                ).generate(parser_request(), collect)
            )
            await asyncio.wait_for(first_delta.wait(), timeout=0.2)
            assert deltas == ['{"location_mention":"']
            assert not task.done()
            stream.release.set()
            result = await task

        assert result.finish_reason is ParserFinishReason.STOP
        assert result.text == '{"location_mention":"河流"}'
        assert deltas == ['{"location_mention":"', '河流"}']
        assert stream.closed.is_set()

    run(exercise())
    assert len(requests) == 1
    request = requests[0]
    payload = json.loads(request.content)
    assert request.method == "POST"
    assert str(request.url) == "https://api.openai.test/v1/responses"
    assert request.headers["accept"] == "text/event-stream"
    assert request.headers["authorization"] == "Bearer openai-secret"
    assert payload == {
        "model": "gpt-parser",
        "instructions": "Return one JSON object.",
        "input": '{"query":"river"}',
        "text": {"format": {"type": "json_object"}},
        "tools": [],
        "store": False,
        "stream": True,
    }


@pytest.mark.parametrize(
    ("chunks", "private_text"),
    [
        (
            (
                sse_event("response.output_text.delta", delta='{"value":"delta"}'),
                output_text_done('{"value":"done"}'),
                completed_terminal('{"value":"delta"}'),
            ),
            '{"value":"done"}',
        ),
        (
            (
                sse_event("response.output_text.delta", delta='{"value":"delta"}'),
                output_text_done('{"value":"delta"}'),
                completed_terminal('{"value":"completed"}'),
            ),
            '{"value":"completed"}',
        ),
        (
            (
                sse_event("response.output_text.delta", delta='{"value":"delta"}'),
                completed_terminal('{"value":"delta"}'),
            ),
            '{"value":"delta"}',
        ),
        (
            (
                sse_event("response.output_text.delta", delta='{"value":"delta"}'),
                output_text_done('{"value":"delta"}'),
                output_text_done('{"value":"delta"}'),
                completed_terminal('{"value":"delta"}'),
            ),
            '{"value":"delta"}',
        ),
        (
            (
                sse_event("response.output_text.delta", delta='{"value":"delta"}'),
                output_text_done('{"value":"delta"}'),
                completed_terminal('{"value":"delta"}'),
                completed_terminal('{"value":"delta"}'),
            ),
            '{"value":"delta"}',
        ),
    ],
    ids=(
        "delta-done-mismatch",
        "done-completed-mismatch",
        "missing-done",
        "duplicate-done",
        "duplicate-terminal",
    ),
)
def test_completed_text_contract_mismatch_is_a_safe_invalid_response(
    chunks: tuple[bytes, ...],
    private_text: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response_for(request, chunks)

    async def exercise() -> None:
        async with client(handler) as http:
            with pytest.raises(InvalidPortResponseError) as captured:
                await OpenAIResponsesParserAdapter(
                    http,
                    api_key="secret",
                    base_url="https://api.openai.test/private",
                ).generate(parser_request(), _noop)
        rendered = f"{captured.value!s} {captured.value!r}"
        assert private_text not in rendered
        assert "api.openai.test" not in rendered

    run(exercise())


def test_completed_terminal_rejects_tool_call_without_leaking_arguments(
    caplog,
) -> None:
    tool_arguments = "terminal-tool-arguments-private"

    def handler(request: httpx.Request) -> httpx.Response:
        return response_for(
            request,
            (
                sse_event("response.output_text.delta", delta="{}"),
                output_text_done("{}"),
                terminal_event(
                    "completed",
                    output=[
                        {"type": "reasoning", "summary": []},
                        {
                            "type": "function_call",
                            "name": "unexpected_tool",
                            "arguments": tool_arguments,
                        },
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "{}"}],
                        },
                    ],
                ),
            ),
        )

    async def exercise() -> None:
        async with client(handler) as http:
            with pytest.raises(InvalidPortResponseError) as captured:
                await OpenAIResponsesParserAdapter(
                    http,
                    api_key="secret",
                    base_url="https://api.openai.test/v1",
                ).generate(parser_request(), _noop)
        rendered = f"{captured.value!s} {captured.value!r}"
        assert tool_arguments not in rendered

    with caplog.at_level(logging.WARNING, logger="wateragents.providers"):
        run(exercise())
    assert tool_arguments not in caplog.text


@pytest.mark.parametrize(
    ("event_type", "item_type"),
    [
        ("response.output_item.added", "function_call"),
        ("response.output_item.done", "web_search_call"),
    ],
    ids=("added-function-call", "done-built-in-tool-call"),
)
def test_output_item_event_rejects_tool_call_without_leaking_arguments(
    event_type: str,
    item_type: str,
    caplog,
) -> None:
    tool_arguments = "streamed-tool-arguments-private"

    def handler(request: httpx.Request) -> httpx.Response:
        return response_for(
            request,
            (
                sse_event(
                    event_type,
                    item={"type": item_type, "arguments": tool_arguments},
                ),
            ),
        )

    async def exercise() -> None:
        async with client(handler) as http:
            with pytest.raises(InvalidPortResponseError) as captured:
                await OpenAIResponsesParserAdapter(
                    http,
                    api_key="secret",
                    base_url="https://api.openai.test/v1",
                ).generate(parser_request(), _noop)
        rendered = f"{captured.value!s} {captured.value!r}"
        assert tool_arguments not in rendered

    with caplog.at_level(logging.WARNING, logger="wateragents.providers"):
        run(exercise())
    assert tool_arguments not in caplog.text


def test_completed_message_rejects_unknown_content_type() -> None:
    private_content = "unknown-content-private"

    def handler(request: httpx.Request) -> httpx.Response:
        return response_for(
            request,
            (
                sse_event("response.output_text.delta", delta="{}"),
                output_text_done("{}"),
                terminal_event(
                    "completed",
                    output=[
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "unknown_content",
                                    "data": private_content,
                                }
                            ],
                        }
                    ],
                ),
            ),
        )

    async def exercise() -> None:
        async with client(handler) as http:
            with pytest.raises(InvalidPortResponseError) as captured:
                await OpenAIResponsesParserAdapter(
                    http,
                    api_key="secret",
                    base_url="https://api.openai.test/v1",
                ).generate(parser_request(), _noop)
        rendered = f"{captured.value!s} {captured.value!r}"
        assert private_content not in rendered

    run(exercise())


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("max_output_tokens", ParserFinishReason.LENGTH),
        ("max_messages", ParserFinishReason.LENGTH),
        ("content_filter", ParserFinishReason.CONTENT_FILTER),
    ],
)
def test_incomplete_terminal_maps_finish_reason(
    reason: str,
    expected: ParserFinishReason,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response_for(
            request,
            (
                sse_event("response.output_text.delta", delta="{"),
                terminal_event(
                    "incomplete",
                    incomplete_details={"reason": reason},
                ),
            ),
        )

    async def exercise() -> None:
        deltas: list[str] = []
        async with client(handler) as http:
            response = await OpenAIResponsesParserAdapter(
                http,
                api_key="secret",
                base_url="https://api.openai.test/v1",
            ).generate(parser_request(), _collector(deltas))
        assert response.text == "{"
        assert response.finish_reason is expected
        assert deltas == ["{"]

    run(exercise())


@pytest.mark.parametrize(
    "response_fields",
    [
        {"incomplete_details": {"reason": "steered"}},
        {"incomplete_details": {"reason": "other"}},
        {"incomplete_details": {"reason": "max_tokens"}},
        {},
        {"incomplete_details": None},
        {"incomplete_details": "invalid"},
        {"incomplete_details": {}},
        {"incomplete_details": {"reason": None}},
        {"incomplete_details": {"reason": 1}},
    ],
    ids=(
        "steered",
        "unknown",
        "removed-max-tokens-alias",
        "details-missing",
        "details-null",
        "details-non-object",
        "reason-missing",
        "reason-null",
        "reason-non-string",
    ),
)
def test_invalid_incomplete_reason_is_a_typed_invalid_response(
    response_fields: dict[str, object],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response_for(
            request,
            (
                sse_event("response.output_text.delta", delta="{"),
                terminal_event("incomplete", **response_fields),
            ),
        )

    async def exercise() -> None:
        async with client(handler) as http:
            with pytest.raises(InvalidPortResponseError):
                await OpenAIResponsesParserAdapter(
                    http,
                    api_key="secret",
                    base_url="https://api.openai.test/v1",
                ).generate(parser_request(), _noop)

    run(exercise())


def test_refusal_is_a_safe_finish_and_is_not_streamed_as_model_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response_for(
            request,
            (
                sse_event("response.refusal.delta", delta="private refusal"),
                sse_event("response.refusal.done", refusal="private refusal"),
                terminal_event(
                    "completed",
                    output=[
                        {
                            "type": "message",
                            "content": [
                                {"type": "refusal", "refusal": "private refusal"}
                            ],
                        }
                    ],
                ),
            ),
        )

    async def exercise() -> None:
        deltas: list[str] = []
        async with client(handler) as http:
            response = await OpenAIResponsesParserAdapter(
                http,
                api_key="secret",
                base_url="https://api.openai.test/v1",
            ).generate(parser_request(), _collector(deltas))
        assert response.text == ""
        assert response.finish_reason is ParserFinishReason.CONTENT_FILTER
        assert deltas == []

    run(exercise())


def test_terminal_refusal_is_a_safe_finish_when_no_refusal_delta_arrives() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response_for(
            request,
            (
                terminal_event(
                    "completed",
                    output=[
                        {
                            "type": "message",
                            "content": [
                                {"type": "refusal", "refusal": "not available"}
                            ],
                        }
                    ],
                ),
            ),
        )

    async def exercise() -> None:
        async with client(handler) as http:
            response = await OpenAIResponsesParserAdapter(
                http,
                api_key="secret",
                base_url="https://api.openai.test/v1",
            ).generate(parser_request(), _noop)
        assert response.text == ""
        assert response.finish_reason is ParserFinishReason.CONTENT_FILTER

    run(exercise())


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_failed_and_cancelled_terminals_are_typed_unavailable(status: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response_for(request, (terminal_event(status),))

    async def exercise() -> None:
        async with client(handler) as http:
            with pytest.raises(PortUnavailableError):
                await OpenAIResponsesParserAdapter(
                    http,
                    api_key="secret",
                    base_url="https://api.openai.test/v1",
                ).generate(parser_request(), _noop)

    run(exercise())


def test_error_event_is_typed_unavailable_without_provider_message() -> None:
    secret_message = "provider-secret-diagnostic"

    def handler(request: httpx.Request) -> httpx.Response:
        return response_for(
            request,
            (sse_event("error", code="server_error", message=secret_message),),
        )

    async def exercise() -> None:
        async with client(handler) as http:
            with pytest.raises(PortUnavailableError) as captured:
                await OpenAIResponsesParserAdapter(
                    http,
                    api_key="secret",
                    base_url="https://api.openai.test/v1",
                ).generate(parser_request(), _noop)
        assert secret_message not in str(captured.value)
        assert secret_message not in repr(captured.value)

    run(exercise())


@pytest.mark.parametrize(
    "chunks",
    [
        (b"event: response.completed\ndata: {\n\n",),
        (b'event: response.created\ndata: {"type":"wrong.event"}\n\n',),
        (b'event: invalid\ndata: {"type":"invalid"}\n\n',),
        (b'event: response.unknown\ndata: {"type":"response.unknown"}\n\n',),
        (b"data: \xff\n\n",),
        (sse_event("response.completed", response={"status": "failed"}),),
        (
            sse_event("response.output_text.delta", delta="{}"),
            sse_event("response.output_text.done", text=1),
            completed_terminal("{}"),
        ),
        (
            sse_event("response.output_text.delta", delta="{}"),
            output_text_done("{}"),
            completed_terminal(1),
        ),
        (sse_event("response.output_text.delta", delta="{"),),
        (b'event: response.completed\ndata: {"type":"response.completed"}',),
    ],
)
def test_invalid_event_or_missing_terminal_is_a_typed_invalid_response(
    chunks: tuple[bytes, ...],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response_for(request, chunks)

    async def exercise() -> None:
        async with client(handler) as http:
            with pytest.raises(InvalidPortResponseError):
                await OpenAIResponsesParserAdapter(
                    http,
                    api_key="secret",
                    base_url="https://api.openai.test/v1",
                ).generate(parser_request(), _noop)

    run(exercise())


def test_non_sse_response_is_a_typed_invalid_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={"status": "completed"},
            request=request,
        )

    async def exercise() -> None:
        async with client(handler) as http:
            with pytest.raises(InvalidPortResponseError):
                await OpenAIResponsesParserAdapter(
                    http,
                    api_key="secret",
                    base_url="https://api.openai.test/v1",
                ).generate(parser_request(), _noop)

    run(exercise())


def test_cancellation_closes_the_response_stream() -> None:
    stream = GatedStream(
        (sse_event("response.output_text.delta", delta="{"),),
        (terminal_event("completed"),),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=stream,
            request=request,
        )

    async def exercise() -> None:
        received = asyncio.Event()

        async def collect(_: str) -> None:
            received.set()

        async with client(handler) as http:
            task = asyncio.create_task(
                OpenAIResponsesParserAdapter(
                    http,
                    api_key="secret",
                    base_url="https://api.openai.test/v1",
                ).generate(parser_request(), collect)
            )
            await asyncio.wait_for(received.wait(), timeout=0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert stream.closed.is_set()

    run(exercise())


def test_timeout_closes_the_response_stream() -> None:
    stream = GatedStream((), (terminal_event("completed"),))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=stream,
            request=request,
        )

    async def exercise() -> None:
        async with client(handler, timeout_seconds=0.01) as http:
            with pytest.raises(PortTimeoutError):
                await OpenAIResponsesParserAdapter(
                    http,
                    api_key="secret",
                    base_url="https://api.openai.test/v1",
                ).generate(parser_request(), _noop)
        assert stream.closed.is_set()

    run(exercise())


def test_streaming_post_failure_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, request=request)

    async def exercise() -> None:
        async with client(handler) as http:
            with pytest.raises(PortHttpError):
                await OpenAIResponsesParserAdapter(
                    http,
                    api_key="secret",
                    base_url="https://api.openai.test/v1",
                ).generate(parser_request(), _noop)

    run(exercise())
    assert calls == 1


def test_response_limit_and_logs_do_not_expose_secrets(caplog) -> None:
    api_key = "openai-private-key"
    prompt_secret = "prompt-private-value"
    raw = f"data: {api_key}-{prompt_secret}-".encode() + (b"x" * 100)

    def handler(request: httpx.Request) -> httpx.Response:
        return response_for(request, (raw,))

    async def exercise() -> None:
        request = QueryParserInputV1(
            model=ModelSelection(provider="openai", model="gpt-parser"),
            messages=(
                ParserPromptMessage(
                    role=ParserMessageRole.SYSTEM,
                    text=prompt_secret,
                ),
                ParserPromptMessage(
                    role=ParserMessageRole.USER,
                    text='{"query":"river"}',
                ),
            ),
        )
        async with client(handler, max_response_bytes=32) as http:
            with pytest.raises(PortResponseLimitError) as captured:
                await OpenAIResponsesParserAdapter(
                    http,
                    api_key=api_key,
                    base_url="https://api.openai.test/private",
                ).generate(request, _noop)
        rendered = f"{captured.value!s} {captured.value!r}"
        assert api_key not in rendered
        assert prompt_secret not in rendered
        assert "private" not in rendered

    with caplog.at_level(logging.WARNING, logger="wateragents.providers"):
        run(exercise())
    assert api_key not in caplog.text
    assert prompt_secret not in caplog.text
    assert "private" not in caplog.text


def test_provider_mismatch_starts_no_http_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, request=request)

    async def exercise() -> None:
        async with client(handler) as http:
            adapter = OpenAIResponsesParserAdapter(
                http,
                api_key="secret",
                base_url="https://api.openai.test/v1",
            )
            with pytest.raises(PortConfigurationError):
                await adapter.generate(parser_request(provider="other"), _noop)

    run(exercise())
    assert calls == 0


def _collector(target: list[str]):
    async def collect(text: str) -> None:
        target.append(text)

    return collect


async def _noop(_: str) -> None:
    return None
