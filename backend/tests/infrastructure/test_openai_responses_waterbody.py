import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from app.application.analysis_runs.workflow import CancellationToken
from app.application.canonical_json import canonical_json_digest
from app.application.reasoning.model_models import (
    StructuredOutputCompleted,
    Terminal,
    ToolCallCompleted,
    UsageReported,
    VisibleTextDelta,
    WaterbodyFinishReason,
    WaterbodyJsonSchema,
    WaterbodyMessageRole,
    WaterbodyModelMessage,
    WaterbodyModelPhase,
    WaterbodyModelRequest,
    WaterbodyToolDescriptor,
)
from app.application.reasoning.model_protocol import consume_waterbody_model_stream
from app.application.run_summary.models import (
    RunSummaryJsonSchema,
    RunSummaryModelMessage,
    RunSummaryModelRequest,
)
from app.application.transcripts.models import TranscriptContentRef
from app.domain import ItemReference, ModelSelection
from app.infrastructure.adapters import OpenAIResponsesWaterbodyAdapter
from app.infrastructure.http import ProviderHttpClient
from app.ports import (
    PortConfigurationError,
    WaterbodyModelCancelledError,
    WaterbodyModelContentFilteredError,
    WaterbodyModelIncompleteError,
    WaterbodyModelProtocolError,
    WaterbodyModelRefusalError,
    WaterbodyModelTimeoutError,
    WaterbodyModelTruncatedError,
    WaterbodyModelUnavailableError,
)

FIXTURES = Path(__file__).with_name("fixtures")
OPERATION_ID = UUID("30000000-0000-4000-8000-000000000010")
MODEL = ModelSelection(provider="openai", model="waterbody-test")


def run(coroutine):
    return asyncio.run(coroutine)


def response_schema() -> WaterbodyJsonSchema:
    return WaterbodyJsonSchema(
        name="evidence_planning_decision",
        schema_text=json.dumps(
            {
                "type": "object",
                "properties": {"decision": {"const": "evidence_ready"}},
                "required": ["decision"],
                "additionalProperties": False,
            }
        ),
    )


def tool_descriptor() -> WaterbodyToolDescriptor:
    return WaterbodyToolDescriptor(
        name="inspect_evidence",
        description="Inspect one server-authorized evidence item.",
        input_schema=WaterbodyJsonSchema(
            name="inspect_evidence",
            schema_text=json.dumps(
                {
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "string", "minLength": 1}
                    },
                    "required": ["item_id"],
                    "additionalProperties": False,
                }
            ),
        ),
    )


def model_request(
    *,
    phase: WaterbodyModelPhase = WaterbodyModelPhase.EVIDENCE_PLANNING,
    provider: str = "openai",
    tools: tuple[WaterbodyToolDescriptor, ...] | None = None,
    messages: tuple[WaterbodyModelMessage, ...] | None = None,
) -> WaterbodyModelRequest:
    selected_tools = (tool_descriptor(),) if tools is None else tools
    return WaterbodyModelRequest(
        operation_id=OPERATION_ID,
        phase=phase,
        model=ModelSelection(provider=provider, model="waterbody-test"),
        system_prompt="Use only the sealed evidence bundle.",
        messages=messages
        or (
            WaterbodyModelMessage(
                message_id="request-message-1",
                role=WaterbodyMessageRole.USER,
                text="Inspect authorized evidence and return the decision.",
            ),
        ),
        tool_descriptors=selected_tools,
        response_schema=response_schema(),
        prompt_policy_digest="sha256:prompt-policy",
        scope_digest="sha256:scope",
        evidence_bundle_ref=ItemReference(item_id="bundle-item-1"),
        batch_id=None if phase is WaterbodyModelPhase.EVIDENCE_PLANNING else "batch-1",
        deadline=datetime.now(UTC) + timedelta(seconds=2),
    )


def summary_model_request() -> RunSummaryModelRequest:
    values = {
        "schema_version": 1,
        "run_id": UUID("10000000-0000-4000-8000-000000000001"),
        "session_id": UUID("20000000-0000-4000-8000-000000000001"),
        "operation_id": OPERATION_ID,
        "model": MODEL,
        "input_ref": TranscriptContentRef(
            run_id=UUID("10000000-0000-4000-8000-000000000001"),
            item_id=UUID("40000000-0000-4000-8000-000000000001"),
        ),
        "input_digest": "sha256:summary-input",
        "system_prompt": "Explain only the supplied run summary facts.",
        "messages": (
            RunSummaryModelMessage(
                message_id="summary-input",
                role="user",
                text='{"target_comid":"300"}',
            ),
        ),
        "response_schema": RunSummaryJsonSchema(
            name="run_summary",
            schema_text=json.dumps(
                {
                    "type": "object",
                    "properties": {"decision": {"const": "evidence_ready"}},
                    "required": ["decision"],
                    "additionalProperties": False,
                }
            ),
        ),
        "deadline": datetime.now(UTC) + timedelta(seconds=2),
    }
    return RunSummaryModelRequest(
        **values,
        request_digest=canonical_json_digest(values),
    )


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def encode_events(events: Iterable[dict[str, object]]) -> bytes:
    frames = []
    for payload in events:
        event_type = payload["type"]
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        frames.append(f"event: {event_type}\r\ndata: {data}\r\n\r\n".encode())
    return b"".join(frames)


class StaticStream(httpx.AsyncByteStream):
    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks = tuple(chunks)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


class GatedStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.release = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        await self.release.wait()
        yield b""

    async def aclose(self) -> None:
        self.closed.set()


def response_for(
    request: httpx.Request,
    chunks: Iterable[bytes],
) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream; charset=utf-8"},
        stream=StaticStream(chunks),
        request=request,
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


async def collect(
    http: ProviderHttpClient,
    request: WaterbodyModelRequest | RunSummaryModelRequest,
    *,
    api_key: str = "fixture-api-key",
    base_url: str = "https://api.openai.test/v1",
):
    adapter = OpenAIResponsesWaterbodyAdapter(
        http,
        api_key=api_key,
        base_url=base_url,
    )
    return tuple([event async for event in adapter.stream(request)])


def test_planning_request_and_fragmented_success_fixture_preserve_safe_order() -> None:
    requests: list[httpx.Request] = []
    raw = encode_events(load_fixture("openai_waterbody_success.json"))
    chunks = tuple(raw[index : index + 7] for index in range(0, len(raw), 7))

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response_for(request, chunks)

    async def exercise():
        async with client(handler) as http:
            return await collect(http, model_request())

    events = run(exercise())
    assert [type(event) for event in events] == [
        VisibleTextDelta,
        ToolCallCompleted,
        ToolCallCompleted,
        VisibleTextDelta,
        StructuredOutputCompleted,
        UsageReported,
        Terminal,
    ]
    assert [event.text for event in events if isinstance(event, VisibleTextDelta)] == [
        "检查已授权证据。",
        '{"decision":"evidence_ready"}',
    ]
    calls = tuple(event for event in events if isinstance(event, ToolCallCompleted))
    assert [(item.provider_call_id, item.name, item.arguments) for item in calls] == [
        ("call-safe-1", "inspect_evidence", '{"item_id":"evidence-1"}'),
        ("call-safe-2", "inspect_evidence", '{"item_id":"evidence-2"}'),
    ]
    structured = next(item for item in events if isinstance(item, StructuredOutputCompleted))
    assert structured.output_id == "message-structured-1"
    assert structured.value == '{"decision":"evidence_ready"}'
    usage = next(item for item in events if isinstance(item, UsageReported))
    assert (usage.input_tokens, usage.output_tokens) == (41, 17)
    terminal = events[-1]
    assert isinstance(terminal, Terminal)
    assert terminal.provider_response_id == "resp-safe-1"
    assert terminal.finish_reason is WaterbodyFinishReason.COMPLETED
    rendered = " ".join(repr(event) for event in events)
    assert "fixture-hidden" not in rendered

    assert len(requests) == 1
    sent = requests[0]
    payload = json.loads(sent.content)
    descriptor = tool_descriptor()
    assert sent.headers["authorization"] == "Bearer fixture-api-key"
    assert str(sent.url) == "https://api.openai.test/v1/responses"
    assert payload == {
        "model": "waterbody-test",
        "instructions": "Use only the sealed evidence bundle.",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": "Inspect authorized evidence and return the decision.",
            }
        ],
        "tools": [
            {
                "type": "function",
                "name": descriptor.name,
                "description": descriptor.description,
                "parameters": json.loads(descriptor.input_schema.schema_text),
                "strict": True,
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "evidence_planning_decision",
                "schema": json.loads(response_schema().schema_text),
                "strict": True,
            }
        },
        "store": False,
        "stream": True,
    }


@pytest.mark.parametrize(
    ("base_url", "deepseek_compat"),
    [
        ("https://api.deepseek.com", True),
        ("https://api.deepseek.com.evil.example", False),
    ],
)
def test_official_deepseek_request_uses_compatible_json_and_tool_format(
    base_url: str,
    deepseek_compat: bool,
) -> None:
    requests: list[httpx.Request] = []
    events = load_fixture("openai_waterbody_success.json")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response_for(request, (encode_events(events),))

    async def exercise():
        async with client(handler) as http:
            return await collect(http, model_request(), base_url=base_url)

    result = run(exercise())
    assert any(isinstance(event, StructuredOutputCompleted) for event in result)
    assert requests[0].url == f"{base_url}/responses"
    payload = json.loads(requests[0].content)
    assert payload["tools"][0]["parameters"] == json.loads(
        tool_descriptor().input_schema.schema_text
    )
    if deepseek_compat:
        assert payload["instructions"].endswith("valid JSON object.")
        assert payload["text"]["format"] == {"type": "json_object"}
        assert "strict" not in payload["tools"][0]
        assert payload["reasoning"] == {"effort": "low"}
        assert payload["max_output_tokens"] == 16_384
    else:
        assert payload["instructions"] == model_request().system_prompt
        assert "reasoning" not in payload
        assert "max_output_tokens" not in payload
        assert payload["text"]["format"] == {
            "type": "json_schema",
            "name": response_schema().name,
            "schema": json.loads(response_schema().schema_text),
            "strict": True,
        }
        assert payload["tools"][0]["strict"] is True


@pytest.mark.parametrize(
    "phase",
    [
        WaterbodyModelPhase.REASONING_BATCH,
        WaterbodyModelPhase.OVERALL_SYNTHESIS,
        WaterbodyModelPhase.SCHEMA_REPAIR,
    ],
)
def test_non_planning_phases_send_exact_empty_tool_roster(
    phase: WaterbodyModelPhase,
) -> None:
    requests: list[httpx.Request] = []
    outcome = load_fixture("openai_waterbody_terminal_outcomes.json")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response_for(request, (encode_events(outcome["completed_without_usage"]),))

    async def exercise():
        async with client(handler) as http:
            return await collect(http, model_request(phase=phase, tools=()))

    events = run(exercise())
    assert not any(isinstance(item, UsageReported) for item in events)
    assert json.loads(requests[0].content)["tools"] == []


def test_run_summary_request_reuses_adapter_with_an_exact_empty_tool_roster() -> None:
    requests: list[httpx.Request] = []
    outcome = load_fixture("openai_waterbody_terminal_outcomes.json")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response_for(request, (encode_events(outcome["completed_without_usage"]),))

    async def exercise():
        async with client(handler) as http:
            return await collect(http, summary_model_request())

    events = run(exercise())
    payload = json.loads(requests[0].content)
    assert isinstance(events[-1], Terminal)
    assert payload["model"] == MODEL.model
    assert payload["tools"] == []
    assert payload["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": '{"target_comid":"300"}',
        }
    ]
    assert payload["text"]["format"]["name"] == "run_summary"


def test_deepseek_summary_schema_mismatch_reaches_application_repair() -> None:
    raw_output = '{"language":"en"}'
    events = [{
        "type": "response.completed",
        "response": {
            "id": "resp-summary-missing-fields",
            "model": MODEL.model,
            "status": "completed",
            "output": [{
                "id": "output-summary-missing-fields",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": raw_output}],
            }],
        },
    }]

    def handler(request: httpx.Request) -> httpx.Response:
        return response_for(request, (encode_events(events),))

    async def exercise():
        async with client(handler) as http:
            return await collect(
                http,
                summary_model_request(),
                base_url="https://api.deepseek.com",
            )

    result = run(exercise())
    assert [type(event) for event in result] == [StructuredOutputCompleted, Terminal]
    assert result[0].value == raw_output
    assert result[1].finish_reason is WaterbodyFinishReason.COMPLETED


@pytest.mark.parametrize(
    "phase",
    [
        WaterbodyModelPhase.REASONING_BATCH,
        WaterbodyModelPhase.OVERALL_SYNTHESIS,
        WaterbodyModelPhase.SCHEMA_REPAIR,
    ],
)
def test_reasoning_schema_mismatch_reaches_application_validation(
    phase: WaterbodyModelPhase,
) -> None:
    raw_output = '{"decision":"not-ready"}'
    events = [{
        "type": "response.completed",
        "response": {
            "id": "resp-reasoning-schema-mismatch",
            "model": MODEL.model,
            "status": "completed",
            "output": [{
                "id": "output-reasoning-schema-mismatch",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": raw_output}],
            }],
        },
    }]

    def handler(request: httpx.Request) -> httpx.Response:
        return response_for(request, (encode_events(events),))

    async def exercise():
        async with client(handler) as http:
            return await collect(
                http,
                model_request(phase=phase, tools=()),
                base_url="https://api.deepseek.com",
            )

    result = run(exercise())
    assert [type(event) for event in result] == [StructuredOutputCompleted, Terminal]
    assert result[0].value == raw_output
    assert result[1].finish_reason is WaterbodyFinishReason.COMPLETED


@pytest.mark.parametrize("raw_output", ["not-json", '["array"]'])
def test_reasoning_repairable_phase_still_rejects_non_object_json(
    raw_output: str,
) -> None:
    events = [{
        "type": "response.completed",
        "response": {
            "id": "resp-invalid-json",
            "model": MODEL.model,
            "status": "completed",
            "output": [{
                "id": "output-invalid-json",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": raw_output}],
            }],
        },
    }]

    def handler(request: httpx.Request) -> httpx.Response:
        return response_for(request, (encode_events(events),))

    async def exercise() -> None:
        async with client(handler) as http:
            await collect(
                http,
                model_request(phase=WaterbodyModelPhase.REASONING_BATCH, tools=()),
                base_url="https://api.deepseek.com",
            )

    with pytest.raises(WaterbodyModelProtocolError):
        run(exercise())


def test_tool_result_message_is_sent_as_function_call_output() -> None:
    requests: list[httpx.Request] = []
    outcome = load_fixture("openai_waterbody_terminal_outcomes.json")
    messages = (
        WaterbodyModelMessage(
            message_id="assistant-1",
            role=WaterbodyMessageRole.ASSISTANT,
            text="Inspecting one item.",
        ),
        WaterbodyModelMessage(
            message_id="tool-1",
            role=WaterbodyMessageRole.TOOL,
            text='{"safe_ref":"item-1"}',
            provider_call_id="call-previous",
            tool_name="inspect_evidence",
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response_for(request, (encode_events(outcome["completed_without_usage"]),))

    async def exercise():
        async with client(handler) as http:
            await collect(http, model_request(messages=messages))

    run(exercise())
    assert json.loads(requests[0].content)["input"] == [
        {"type": "message", "role": "assistant", "content": "Inspecting one item."},
        {
            "type": "function_call_output",
            "call_id": "call-previous",
            "output": '{"safe_ref":"item-1"}',
        },
    ]


def test_assistant_tool_call_is_sent_with_matching_function_output() -> None:
    from app.infrastructure.adapters.openai_responses_waterbody import _request_body

    call = WaterbodyModelMessage(message_id="lookup-call", role="assistant", text='{"query":"pH buffer capacity"}',
        provider_call_id="lookup-1", tool_name="search_guidelines")
    result = WaterbodyModelMessage(message_id="lookup-result", role="tool", text='{"status":"no_match"}',
        provider_call_id="lookup-1", tool_name="search_guidelines")
    body = _request_body(model_request(messages=(call, result)))
    assert body["input"] == [
        {"type": "function_call", "call_id": "lookup-1", "name": "search_guidelines", "arguments": call.text},
        {"type": "function_call_output", "call_id": "lookup-1", "output": result.text},
    ]
    with pytest.raises(ValueError):
        WaterbodyModelMessage(message_id="partial", role="assistant", text='{}', tool_name="search_guidelines")
    with pytest.raises(ValueError):
        WaterbodyModelMessage(message_id="invalid", role="assistant", text='not JSON',
            tool_name="search_guidelines", provider_call_id="lookup-1")


@pytest.mark.parametrize(
    ("fixture_name", "expected_error"),
    [
        ("incomplete", WaterbodyModelIncompleteError),
        ("truncated", WaterbodyModelTruncatedError),
        ("content_filtered", WaterbodyModelContentFilteredError),
        ("refused", WaterbodyModelRefusalError),
        ("cancelled", WaterbodyModelCancelledError),
        ("failed", WaterbodyModelUnavailableError),
    ],
)
def test_terminal_outcomes_map_deterministically_to_m6_errors(
    fixture_name: str,
    expected_error: type[Exception],
) -> None:
    outcome = load_fixture("openai_waterbody_terminal_outcomes.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return response_for(request, (encode_events(outcome[fixture_name]),))

    async def exercise() -> None:
        async with client(handler) as http:
            adapter = OpenAIResponsesWaterbodyAdapter(
                http,
                api_key="fixture-api-key",
                base_url="https://api.openai.test/v1",
            )
            await consume_waterbody_model_stream(
                adapter,
                model_request(),
                cancellation=CancellationToken(asyncio.Event()),
            )

    with pytest.raises(expected_error):
        run(exercise())


@pytest.mark.parametrize(
    "case",
    [
        "unknown_event",
        "unknown_item",
        "unknown_content",
        "built_in_tool",
        "model_drift",
        "response_id_drift",
        "schema_mismatch",
        "duplicate_structured_key",
    ],
)
def test_checked_invalid_fixtures_are_typed_protocol_errors(case: str) -> None:
    fixtures = load_fixture("openai_waterbody_invalid.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return response_for(request, (encode_events(fixtures[case]),))

    async def exercise() -> None:
        async with client(handler) as http:
            await collect(http, model_request())

    with pytest.raises(WaterbodyModelProtocolError):
        run(exercise())


def test_deepseek_json_mode_still_checks_the_original_response_schema() -> None:
    events = load_fixture("openai_waterbody_invalid.json")["schema_mismatch"]

    def handler(request: httpx.Request) -> httpx.Response:
        return response_for(request, (encode_events(events),))

    async def exercise() -> None:
        async with client(handler) as http:
            await collect(
                http,
                model_request(),
                base_url="https://api.deepseek.com",
            )

    with pytest.raises(WaterbodyModelProtocolError):
        run(exercise())


def test_tool_call_with_whitespace_only_text_does_not_require_structured_output() -> None:
    tool_item = {
        "id": "function-with-whitespace",
        "type": "function_call",
        "call_id": "call-with-whitespace",
        "name": "inspect_evidence",
        "arguments": '{"item_id":"evidence-1"}',
    }
    whitespace_message = {
        "id": "message-with-whitespace",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "   \n  "}],
    }

    def events(output: list[dict[str, object]]) -> list[dict[str, object]]:
        return [{
            "type": "response.completed",
            "response": {
                "id": "resp-with-whitespace",
                "model": "waterbody-test",
                "status": "completed",
                "output": output,
            },
        }]

    def handler(request: httpx.Request) -> httpx.Response:
        return response_for(request, (encode_events(events([whitespace_message, tool_item])),))

    async def exercise():
        async with client(handler) as http:
            return await collect(http, model_request(), base_url="https://api.deepseek.com")

    result = run(exercise())
    assert [type(item) for item in result] == [ToolCallCompleted, Terminal]
    assert result[0].provider_call_id == "call-with-whitespace"
    assert result[1].finish_reason is WaterbodyFinishReason.COMPLETED

    def no_tool_handler(request: httpx.Request) -> httpx.Response:
        return response_for(request, (encode_events(events([whitespace_message])),))

    async def no_tool_exercise() -> None:
        async with client(no_tool_handler) as http:
            await collect(http, model_request(), base_url="https://api.deepseek.com")

    with pytest.raises(WaterbodyModelProtocolError):
        run(no_tool_exercise())


@pytest.mark.parametrize(
    ("window_ids", "base_url", "arguments", "expected"),
    [
        (("window-000",), "https://api.deepseek.com", None, True),
        (("window-000",), "https://api.openai.test/v1", None, False),
        (("window-000", "window-001"), "https://api.deepseek.com", None, False),
        (("window-000",), "https://api.deepseek.com", "extra_option", False),
        (("window-000",), "https://api.deepseek.com", "wrong_window", False),
        (("window-000",), "https://api.deepseek.com", "missing_parameter", False),
        (("window-000",), "https://api.deepseek.com", "raw_mismatch", False),
    ],
)
def test_deepseek_tool_can_fill_only_an_unambiguous_missing_window(
    window_ids: tuple[str, ...],
    base_url: str,
    arguments: str | None,
    expected: bool,
) -> None:
    schema = {
        "type": "object",
        "properties": {
            "window_ids": {
                "type": "array",
                "items": {"type": "string", "enum": window_ids},
                "minItems": 1,
            },
            "parameter_keys": {
                "type": "array",
                "items": {"type": "object"},
                "minItems": 1,
            },
            "options": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        "required": ["window_ids", "parameter_keys", "options"],
        "additionalProperties": False,
    }
    descriptor = WaterbodyToolDescriptor(
        name="collect_weather_evidence",
        description="Collect authorized weather evidence.",
        input_schema=WaterbodyJsonSchema(
            name="collect_weather_evidence",
            schema_text=json.dumps(schema),
        ),
    )
    tool_args = {"parameter_keys": [{"code_system": "USGS", "code": "00011"}], "options": {}}
    if arguments == "extra_option":
        tool_args["options"] = {"unapproved": True}
    elif arguments == "wrong_window":
        tool_args["window_ids"] = ["window-other"]
    elif arguments == "missing_parameter":
        del tool_args["parameter_keys"]
    raw = json.dumps(tool_args, separators=(",", ":"))
    item = {
        "id": "weather-tool-item",
        "type": "function_call",
        "call_id": "weather-call",
        "name": descriptor.name,
        "arguments": raw,
    }
    if arguments == "raw_mismatch":
        item["arguments"] = json.dumps(tool_args, indent=1)
    events = [
        {
            "type": "response.created",
            "response": {"id": "resp-weather", "model": MODEL.model, "status": "in_progress"},
        },
        {
            "type": "response.output_item.added",
            "item": {key: item[key] for key in ("id", "type", "call_id", "name")},
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": item["id"],
            "arguments": raw,
        },
        {"type": "response.output_item.done", "item": item},
        {
            "type": "response.completed",
            "response": {
                "id": "resp-weather",
                "model": MODEL.model,
                "status": "completed",
                "output": [item],
            },
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return response_for(request, (encode_events(events),))

    async def exercise():
        async with client(handler) as http:
            return await collect(
                http, model_request(tools=(descriptor,)), base_url=base_url
            )

    if not expected:
        with pytest.raises(WaterbodyModelProtocolError):
            run(exercise())
        return
    outcome = run(exercise())
    assert [type(event) for event in outcome] == [ToolCallCompleted, Terminal]
    assert json.loads(outcome[0].arguments) == {
        **tool_args,
        "window_ids": ["window-000"],
    }


@pytest.mark.parametrize(
    ("raw", "base_url", "done_matches", "expected_path"),
    [
        (
            '{"cells":[{"trend":null,"trend":null,"note":"private-visible-text"}]}',
            "https://api.deepseek.com",
            True,
            "$.cells[0].trend",
        ),
        (
            '{"meta":{"a":1,"b":2},"meta":{"b":2,"a":1}}',
            "https://api.deepseek.com",
            True,
            "$.meta",
        ),
        (
            '{"cells":[{"trend":null,"trend":"rising"}]}',
            "https://api.deepseek.com",
            True,
            None,
        ),
        (
            '{"cells":[{"trend":null,"trend":null}]}',
            "https://api.openai.test/v1",
            True,
            None,
        ),
        (
            '{"cells":[{"trend":null,"trend":null}]}',
            "https://api.deepseek.com",
            False,
            None,
        ),
    ],
)
def test_duplicate_json_keys_are_canonicalized_only_when_unambiguous_on_deepseek(
    raw: str,
    base_url: str,
    done_matches: bool,
    expected_path: str | None,
    caplog,
) -> None:
    item = {
        "id": "output-duplicate-key",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": raw}],
    }
    events = [
        {
            "type": "response.created",
            "response": {"id": "resp-duplicate-key", "model": MODEL.model, "status": "in_progress"},
        },
        {
            "type": "response.output_item.added",
            "item": {"id": item["id"], "type": "message", "role": "assistant", "content": []},
        },
        {
            "type": "response.output_text.done",
            "item_id": item["id"],
            "text": raw if done_matches else raw + " ",
        },
        {"type": "response.output_item.done", "item": item},
        {
            "type": "response.completed",
            "response": {
                "id": "resp-duplicate-key",
                "model": MODEL.model,
                "status": "completed",
                "output": [item],
            },
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return response_for(request, (encode_events(events),))

    async def exercise():
        async with client(handler) as http:
            return await collect(
                http,
                model_request(phase=WaterbodyModelPhase.SCHEMA_REPAIR, tools=()),
                base_url=base_url,
            )

    with caplog.at_level(logging.INFO, logger="wateragents.providers"):
        if expected_path is None:
            with pytest.raises(WaterbodyModelProtocolError):
                run(exercise())
            assert not any(
                record.message == "provider_output_duplicate_keys_canonicalized"
                for record in caplog.records
            )
            return
        result = run(exercise())
    assert [type(event) for event in result] == [StructuredOutputCompleted, Terminal]
    assert json.loads(result[0].value) == json.loads(raw)
    assert len(result[0].value) < len(raw)
    canonicalization = next(
        record for record in caplog.records
        if record.message == "provider_output_duplicate_keys_canonicalized"
    )
    assert canonicalization.duplicate_key_count == 1
    assert canonicalization.duplicate_key_paths == [expected_path]
    assert "private-visible-text" not in caplog.text


def test_protocol_failure_logs_event_metadata_without_model_text(caplog) -> None:
    private_text = "private-model-output-should-stay-out-of-logs"
    events = [{
        "type": "response.output_text.delta",
        "item_id": "missing-added-item",
        "delta": private_text,
    }]

    def handler(request: httpx.Request) -> httpx.Response:
        return response_for(request, (encode_events(events),))

    async def exercise() -> None:
        async with client(handler) as http:
            await collect(http, model_request())

    with caplog.at_level(logging.WARNING, logger="wateragents.providers"):
        with pytest.raises(WaterbodyModelProtocolError):
            run(exercise())

    diagnostic = next(
        record for record in caplog.records
        if record.message == "provider_stream_protocol_invalid"
    )
    assert diagnostic.stream_event_type == "response.output_text.delta"
    assert diagnostic.stream_payload_keys == ["delta", "item_id", "type"]
    assert diagnostic.stream_failure_rule.startswith("_require_item_type:")
    assert private_text not in caplog.text


def test_failed_stream_logs_error_code_without_provider_message(caplog) -> None:
    private_text = "private-provider-message-should-stay-out-of-logs"
    events = [{
        "type": "response.failed",
        "response": {
            "id": "response-failed",
            "model": "waterbody-test",
            "status": "failed",
            "error": {
                "type": "api_error",
                "code": "server_error",
                "message": private_text,
            },
        },
    }]

    def handler(request: httpx.Request) -> httpx.Response:
        return response_for(request, (encode_events(events),))

    async def exercise() -> None:
        async with client(handler) as http:
            await collect(http, model_request())

    with caplog.at_level(logging.WARNING, logger="wateragents.providers"):
        with pytest.raises(WaterbodyModelUnavailableError):
            run(exercise())

    diagnostic = next(
        record for record in caplog.records
        if record.message == "provider_stream_failed"
    )
    assert diagnostic.stream_event_type == "response.failed"
    assert diagnostic.stream_error_type == "api_error"
    assert diagnostic.stream_error_code == "server_error"
    assert private_text not in caplog.text


@pytest.mark.parametrize("case", ["malformed_json_sse", "incomplete_sse"])
def test_malformed_or_incomplete_sse_is_a_typed_protocol_error(case: str) -> None:
    fixtures = load_fixture("openai_waterbody_invalid.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return response_for(request, (fixtures[case].encode(),))

    async def exercise() -> None:
        async with client(handler) as http:
            await collect(http, model_request())

    with pytest.raises(WaterbodyModelProtocolError):
        run(exercise())


def test_duplicate_missing_and_post_terminal_events_are_rejected() -> None:
    outcome = load_fixture("openai_waterbody_terminal_outcomes.json")
    completed = outcome["completed_without_usage"]
    cases = (
        completed + completed,
        [],
        completed
        + [
            {
                "type": "response.output_text.delta",
                "item_id": "output-complete",
                "delta": "late-private-output",
            }
        ],
    )

    for events in cases:
        def handler(request: httpx.Request, current=events) -> httpx.Response:
            return response_for(request, (encode_events(current),))

        async def exercise() -> None:
            async with client(handler) as http:
                await collect(http, model_request())

        with pytest.raises(WaterbodyModelProtocolError):
            run(exercise())


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("inspect_evidence", ""),
        ("inspect_evidence", "not-json"),
        ("inspect_evidence", "{}"),
        ("inspect_evidence", '{"item_id":"one","extra":true}'),
        ("inspect_evidence", '{"item_id":"","item_id":"one"}'),
        ("invented_tool", '{"item_id":"one"}'),
    ],
)
def test_tool_arguments_and_roster_are_strictly_bounded(
    name: str,
    arguments: str,
) -> None:
    events = [
        {
            "type": "response.completed",
            "response": {
                "id": "resp-tool-invalid",
                "model": "waterbody-test",
                "status": "completed",
                "output": [
                    {
                        "id": "function-invalid",
                        "type": "function_call",
                        "call_id": "call-invalid",
                        "name": name,
                        "arguments": arguments,
                    }
                ],
            },
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return response_for(request, (encode_events(events),))

    async def exercise() -> None:
        async with client(handler) as http:
            await collect(http, model_request())

    with pytest.raises(WaterbodyModelProtocolError):
        run(exercise())


@pytest.mark.parametrize(
    ("arguments", "schema_path", "schema_rule"),
    [
        ("{}", "$.item_id", "required"),
        ('{"item_id":""}', "$.item_id", "minLength"),
        (
            '{"item_id":"private-item-value","unexpected":"private-argument-value"}',
            "$",
            "additionalProperties",
        ),
    ],
)
def test_invalid_tool_arguments_log_schema_rule_without_argument_values(
    arguments: str,
    schema_path: str,
    schema_rule: str,
    caplog,
) -> None:
    events = [
        {
            "type": "response.completed",
            "response": {
                "id": "resp-tool-invalid",
                "model": "waterbody-test",
                "status": "completed",
                "output": [
                    {
                        "id": "function-invalid",
                        "type": "function_call",
                        "call_id": "call-invalid",
                        "name": "inspect_evidence",
                        "arguments": arguments,
                    }
                ],
            },
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return response_for(request, (encode_events(events),))

    async def exercise() -> None:
        async with client(handler) as http:
            await collect(http, model_request())

    with caplog.at_level(logging.WARNING, logger="wateragents.providers"):
        with pytest.raises(WaterbodyModelProtocolError):
            run(exercise())
    mismatch = next(
        record for record in caplog.records
        if record.getMessage() == "provider_schema_mismatch"
    )
    assert mismatch.tool_name == "inspect_evidence"
    assert mismatch.schema_name == "inspect_evidence"
    assert mismatch.schema_path == schema_path
    assert mismatch.schema_rule == schema_rule
    assert "private-item-value" not in caplog.text
    assert "private-argument-value" not in caplog.text
    assert "unexpected" not in caplog.text


def test_http_timeout_network_failure_and_task_cancel_are_typed() -> None:
    timeout_stream = GatedStream()

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=timeout_stream,
            request=request,
        )

    async def timeout_exercise() -> None:
        async with client(timeout_handler, timeout_seconds=0.01) as http:
            await collect(http, model_request())

    with pytest.raises(WaterbodyModelTimeoutError):
        run(timeout_exercise())
    assert timeout_stream.closed.is_set()

    def network_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private-network-diagnostic", request=request)

    async def network_exercise() -> None:
        async with client(network_handler) as http:
            await collect(http, model_request())

    with pytest.raises(WaterbodyModelUnavailableError):
        run(network_exercise())

    cancel_stream = GatedStream()

    def cancel_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=cancel_stream,
            request=request,
        )

    async def cancel_exercise() -> None:
        async with client(cancel_handler) as http:
            task = asyncio.create_task(collect(http, model_request()))
            await asyncio.wait_for(cancel_stream.started.wait(), timeout=0.2)
            task.cancel()
            with pytest.raises(WaterbodyModelCancelledError):
                await task
        assert cancel_stream.closed.is_set()

    run(cancel_exercise())


def test_secrets_raw_provider_content_and_arguments_do_not_leak(caplog) -> None:
    fixtures = load_fixture("openai_waterbody_invalid.json")
    api_key = "fixture-private-api-key"
    prompt = "fixture-private-prompt"
    base_url = "https://api.openai.test/v1?api_key=fixture-private-query"
    request = model_request(
        messages=(
            WaterbodyModelMessage(
                message_id="request-private",
                role=WaterbodyMessageRole.USER,
                text=prompt,
            ),
        )
    )

    def handler(http_request: httpx.Request) -> httpx.Response:
        return response_for(http_request, (encode_events(fixtures["built_in_tool"]),))

    async def exercise() -> None:
        async with client(handler) as http:
            with pytest.raises(WaterbodyModelProtocolError) as captured:
                await collect(
                    http,
                    request,
                    api_key=api_key,
                    base_url=base_url,
                )
        rendered = f"{captured.value!s} {captured.value!r}"
        for private in (
            api_key,
            prompt,
            "fixture-private-query",
            "fixture-private-tool-arguments",
            "fixture-private-envelope",
        ):
            assert private not in rendered

    with caplog.at_level(logging.WARNING, logger="wateragents.providers"):
        run(exercise())
    for private in (
        api_key,
        prompt,
        "fixture-private-query",
        "fixture-private-tool-arguments",
        "fixture-private-envelope",
    ):
        assert private not in caplog.text


def test_configuration_and_provider_mismatch_start_no_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response_for(request, ())

    async def exercise() -> None:
        async with client(handler) as http:
            with pytest.raises(PortConfigurationError):
                OpenAIResponsesWaterbodyAdapter(
                    http,
                    api_key="",
                    base_url="https://api.openai.test/v1",
                )
            adapter = OpenAIResponsesWaterbodyAdapter(
                http,
                api_key="fixture-api-key",
                base_url="https://api.openai.test/v1",
            )
            with pytest.raises(PortConfigurationError):
                tuple(
                    [
                        event
                        async for event in adapter.stream(
                            model_request(provider="another-provider")
                        )
                    ]
                )

    run(exercise())
    assert calls == 0
