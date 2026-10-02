import asyncio
from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.application.analysis_runs.workflow import CancellationToken
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
    WaterbodyModelMessage,
    WaterbodyModelPhase,
    WaterbodyModelRequest,
    WaterbodyToolDescriptor,
    parse_waterbody_model_event,
)
from app.application.reasoning.model_protocol import (
    consume_waterbody_model_stream,
    to_waterbody_model_public_error,
)
from app.domain import ItemReference, ModelSelection
from app.ports import (
    WaterbodyModelCancelledError,
    WaterbodyModelContentFilteredError,
    WaterbodyModelIncompleteError,
    WaterbodyModelProtocolError,
    WaterbodyModelRefusalError,
    WaterbodyModelTimeoutError,
    WaterbodyModelTruncatedError,
)
from tests.application.reasoning.fakes import (
    DeterministicWaterbodyModelPort,
    ScriptedCancellation,
)

OPERATION_ID = UUID("30000000-0000-4000-8000-000000000001")
ERROR_ID = UUID("90000000-0000-4000-8000-000000000001")
MODEL = ModelSelection(provider="openai", model="waterbody-test")
OTHER_MODEL = ModelSelection(provider="openai", model="other-model")


def schema(name: str = "reasoning_result") -> WaterbodyJsonSchema:
    return WaterbodyJsonSchema(
        name=name,
        schema_text='{"type":"object","additionalProperties":false}',
    )


def request(**changes: object) -> WaterbodyModelRequest:
    values: dict[str, object] = {
        "operation_id": OPERATION_ID,
        "phase": WaterbodyModelPhase.REASONING_BATCH,
        "model": MODEL,
        "system_prompt": "Use only the supplied evidence.",
        "messages": (
            WaterbodyModelMessage(
                message_id="message-1",
                role=WaterbodyMessageRole.USER,
                text="Assess the supported cells.",
            ),
        ),
        "tool_descriptors": (),
        "response_schema": schema(),
        "prompt_policy_digest": "sha256:prompt-policy",
        "scope_digest": "sha256:scope",
        "evidence_bundle_ref": ItemReference(item_id="bundle-item-1"),
        "batch_id": "batch-1",
        "deadline": datetime(2026, 10, 2, 12, tzinfo=timezone.utc),
    }
    values.update(changes)
    return WaterbodyModelRequest.model_validate(values)


def event_kwargs(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "operation_id": OPERATION_ID,
        "model": MODEL,
    }
    values.update(changes)
    return values


def completed_terminal(
    finish_reason: WaterbodyFinishReason = WaterbodyFinishReason.COMPLETED,
) -> Terminal:
    return Terminal(
        **event_kwargs(),
        provider_response_id="response-1",
        finish_reason=finish_reason,
    )


def token(event: asyncio.Event | None = None) -> CancellationToken:
    return CancellationToken(event or asyncio.Event())


def consume(
    *steps: object,
    cancellation: CancellationToken | None = None,
):
    fake = DeterministicWaterbodyModelPort(tuple(steps))
    result = asyncio.run(
        consume_waterbody_model_stream(
            fake,
            request(),
            cancellation=cancellation or token(),
        )
    )
    return fake, result


def test_each_legal_event_round_trips_strictly() -> None:
    events = (
        VisibleTextDelta(**event_kwargs(), message_id="message-a", text="first"),
        ToolCallCompleted(
            **event_kwargs(),
            provider_call_id="call-a",
            name="inspect_evidence_records",
            arguments='{"item_id":"evidence-1"}',
        ),
        StructuredOutputCompleted(
            **event_kwargs(),
            output_id="output-a",
            value='{"decision_type":"reasoning_batch"}',
        ),
        RefusalCompleted(
            **event_kwargs(),
            message_id="refusal-a",
            text="I cannot process this request.",
        ),
        UsageReported(**event_kwargs(), input_tokens=12, output_tokens=7),
        completed_terminal(),
    )

    for event in events:
        dumped = event.model_dump(mode="json")
        assert parse_waterbody_model_event(dumped) == event


def test_visible_text_deltas_preserve_whitespace_chunks() -> None:
    delta = VisibleTextDelta(
        **event_kwargs(),
        message_id="message-a",
        text=" ",
    )

    assert delta.text == " "
    assert parse_waterbody_model_event(delta.model_dump(mode="json")) == delta


def test_request_contains_typed_safe_messages_tools_schema_and_identity() -> None:
    descriptor = WaterbodyToolDescriptor(
        name="inspect_evidence_records",
        description="Read an authorized evidence page.",
        input_schema=schema("inspect_evidence_records"),
    )
    model_request = request(
        phase=WaterbodyModelPhase.EVIDENCE_PLANNING,
        messages=(
            WaterbodyModelMessage(
                message_id="message-1",
                role=WaterbodyMessageRole.USER,
                text="Inspect evidence.",
            ),
            WaterbodyModelMessage(
                message_id="message-2",
                role=WaterbodyMessageRole.TOOL,
                text="Evidence page reference: item-1.",
                provider_call_id="call-1",
                tool_name="inspect_evidence_records",
            ),
        ),
        tool_descriptors=(descriptor,),
        batch_id=None,
    )

    assert model_request.operation_id == OPERATION_ID
    assert model_request.evidence_bundle_ref.item_id == "bundle-item-1"
    assert model_request.tool_descriptors == (descriptor,)


@pytest.mark.parametrize(
    "invalid_event",
    [
        {**event_kwargs(), "event_type": "unknown", "text": "value"},
        {
            **event_kwargs(),
            "event_type": "visible_text_delta",
            "message_id": "message-a",
            "text": "",
        },
        {
            **event_kwargs(),
            "event_type": "tool_call_completed",
            "provider_call_id": "call-a",
            "name": "inspect_evidence_records",
            "arguments": "not-json",
        },
        {
            **event_kwargs(),
            "event_type": "usage_reported",
            "input_tokens": -1,
            "output_tokens": 2,
        },
        {
            **event_kwargs(),
            "event_type": "usage_reported",
            "input_tokens": True,
            "output_tokens": 2,
        },
        {
            **event_kwargs(),
            "event_type": "visible_text_delta",
            "message_id": "message-a",
            "text": "safe",
            "api_key": "private",
        },
    ],
)
def test_event_parser_rejects_unknown_empty_corrupt_negative_bool_and_extra_secret(
    invalid_event: object,
) -> None:
    with pytest.raises(ValidationError):
        parse_waterbody_model_event(invalid_event)


@pytest.mark.parametrize(
    "json_text",
    [
        '{"value":1,"value":2}',
        '{"api_key":"private"}',
        "[]",
        '{"value":NaN}',
    ],
)
def test_json_boundaries_reject_duplicate_secret_non_object_and_non_finite_values(
    json_text: str,
) -> None:
    with pytest.raises(ValidationError):
        ToolCallCompleted(
            **event_kwargs(),
            provider_call_id="call-a",
            name="inspect_evidence_records",
            arguments=json_text,
        )


def test_consumer_preserves_multi_message_tool_structured_order_and_identity() -> None:
    steps = (
        VisibleTextDelta(**event_kwargs(), message_id="message-a", text="First "),
        VisibleTextDelta(**event_kwargs(), message_id="message-a", text="message."),
        ToolCallCompleted(
            **event_kwargs(),
            provider_call_id="call-a",
            name="inspect_evidence_records",
            arguments='{"item_id":"evidence-1"}',
        ),
        VisibleTextDelta(**event_kwargs(), message_id="message-b", text="Second message."),
        StructuredOutputCompleted(
            **event_kwargs(),
            output_id="output-a",
            value='{"decision_type":"reasoning_batch"}',
        ),
        UsageReported(**event_kwargs(), input_tokens=20, output_tokens=10),
        completed_terminal(),
    )

    fake, response = consume(*steps)

    assert fake.calls == [request()]
    assert fake.emitted_events == list(steps)
    assert response.operation_id == OPERATION_ID
    assert response.provider_response_id == "response-1"
    assert response.events == steps[:5]
    assert response.events[0].text == "First "
    assert response.usage is not None
    assert response.usage.input_tokens == 20
    assert response.usage.output_tokens == 10
    assert type(response).model_validate(response.model_dump(mode="json")) == response


def test_consumer_allows_missing_usage_as_null() -> None:
    _, response = consume(
        VisibleTextDelta(**event_kwargs(), message_id="message-a", text="Complete."),
        completed_terminal(),
    )

    assert response.usage is None


@pytest.mark.parametrize(
    "steps",
    [
        (
            StructuredOutputCompleted(
                **event_kwargs(), output_id="a", value='{"value":1}'
            ),
            StructuredOutputCompleted(
                **event_kwargs(), output_id="b", value='{"value":2}'
            ),
            completed_terminal(),
        ),
        (
            VisibleTextDelta(**event_kwargs(), message_id="a", text="value"),
            completed_terminal(),
            VisibleTextDelta(**event_kwargs(), message_id="b", text="late"),
        ),
        (
            VisibleTextDelta(**event_kwargs(), message_id="a", text="value"),
            completed_terminal(),
            completed_terminal(),
        ),
        (
            UsageReported(**event_kwargs(), input_tokens=1),
            UsageReported(**event_kwargs(), output_tokens=1),
            completed_terminal(),
        ),
        (
            VisibleTextDelta(**event_kwargs(), message_id="a", text="value"),
        ),
        (
            VisibleTextDelta(
                **event_kwargs(model=OTHER_MODEL),
                message_id="a",
                text="value",
            ),
            completed_terminal(),
        ),
        (
            VisibleTextDelta(
                **event_kwargs(
                    operation_id=UUID("30000000-0000-4000-8000-000000000099")
                ),
                message_id="a",
                text="value",
            ),
            completed_terminal(),
        ),
    ],
)
def test_consumer_rejects_duplicate_out_of_order_missing_and_identity_drift(
    steps: tuple[object, ...],
) -> None:
    fake = DeterministicWaterbodyModelPort(steps)

    with pytest.raises(WaterbodyModelProtocolError):
        asyncio.run(
            consume_waterbody_model_stream(fake, request(), cancellation=token())
        )


def test_consumer_rejects_raw_unknown_event_without_returning_a_response() -> None:
    fake = DeterministicWaterbodyModelPort(
        (
            ToolCallCompleted(
                **event_kwargs(),
                provider_call_id="call-a",
                name="inspect_evidence_records",
                arguments='{"item_id":"evidence-1"}',
            ),
            {**event_kwargs(), "event_type": "provider_sdk_item"},
            completed_terminal(),
        )
    )

    with pytest.raises(WaterbodyModelProtocolError):
        asyncio.run(
            consume_waterbody_model_stream(fake, request(), cancellation=token())
        )

    assert len(fake.calls) == 1


def test_scripted_cancellation_stops_at_boundary_and_discards_late_events() -> None:
    cancellation_event = asyncio.Event()
    late_event = VisibleTextDelta(
        **event_kwargs(), message_id="message-b", text="late output"
    )
    fake = DeterministicWaterbodyModelPort(
        (
            VisibleTextDelta(
                **event_kwargs(), message_id="message-a", text="accepted before cancel"
            ),
            ScriptedCancellation(cancellation_event),
            late_event,
            completed_terminal(),
        )
    )

    with pytest.raises(WaterbodyModelCancelledError):
        asyncio.run(
            consume_waterbody_model_stream(
                fake,
                request(),
                cancellation=token(cancellation_event),
            )
        )

    assert fake.emitted_events[-1] == late_event
    assert completed_terminal() not in fake.emitted_events


@pytest.mark.parametrize(
    ("finish_reason", "error_type"),
    [
        (WaterbodyFinishReason.CONTENT_FILTERED, WaterbodyModelContentFilteredError),
        (WaterbodyFinishReason.TRUNCATED, WaterbodyModelTruncatedError),
        (WaterbodyFinishReason.INCOMPLETE, WaterbodyModelIncompleteError),
        (WaterbodyFinishReason.CANCELLED, WaterbodyModelCancelledError),
    ],
)
def test_terminal_finish_reasons_map_to_typed_errors(
    finish_reason: WaterbodyFinishReason,
    error_type: type[Exception],
) -> None:
    fake = DeterministicWaterbodyModelPort(
        (
            VisibleTextDelta(**event_kwargs(), message_id="a", text="partial"),
            completed_terminal(finish_reason),
        )
    )

    with pytest.raises(error_type):
        asyncio.run(
            consume_waterbody_model_stream(fake, request(), cancellation=token())
        )


@pytest.mark.parametrize(
    ("finish_reason", "error_type"),
    [
        (WaterbodyFinishReason.CONTENT_FILTERED, WaterbodyModelContentFilteredError),
        (WaterbodyFinishReason.TRUNCATED, WaterbodyModelTruncatedError),
        (WaterbodyFinishReason.INCOMPLETE, WaterbodyModelIncompleteError),
        (WaterbodyFinishReason.CANCELLED, WaterbodyModelCancelledError),
    ],
)
def test_failure_terminal_without_visible_content_remains_distinct(
    finish_reason: WaterbodyFinishReason,
    error_type: type[Exception],
) -> None:
    fake = DeterministicWaterbodyModelPort((completed_terminal(finish_reason),))

    with pytest.raises(error_type):
        asyncio.run(
            consume_waterbody_model_stream(fake, request(), cancellation=token())
        )


def test_refusal_requires_refusal_event_and_maps_to_typed_error() -> None:
    fake = DeterministicWaterbodyModelPort(
        (
            RefusalCompleted(
                **event_kwargs(), message_id="refusal-a", text="Request refused."
            ),
            completed_terminal(WaterbodyFinishReason.REFUSED),
        )
    )

    with pytest.raises(WaterbodyModelRefusalError):
        asyncio.run(
            consume_waterbody_model_stream(fake, request(), cancellation=token())
        )


def test_scripted_typed_error_preserves_safe_identity() -> None:
    error = WaterbodyModelTimeoutError(
        provider=MODEL.provider,
        model=MODEL.model,
        operation_id=str(OPERATION_ID),
    )
    fake = DeterministicWaterbodyModelPort((error,))

    with pytest.raises(WaterbodyModelTimeoutError) as raised:
        asyncio.run(
            consume_waterbody_model_stream(fake, request(), cancellation=token())
        )

    assert raised.value is error


def test_public_error_contains_only_safe_identity_and_classification() -> None:
    model_request = request()
    error = WaterbodyModelTimeoutError(
        provider=MODEL.provider,
        model=MODEL.model,
        operation_id=str(OPERATION_ID),
    )

    public = to_waterbody_model_public_error(
        error,
        model_request,
        error_id=ERROR_ID,
    )

    assert public.model_dump(mode="json") == {
        "error_id": str(ERROR_ID),
        "provider": "openai",
        "model": "waterbody-test",
        "code": "waterbody_model_timeout",
        "failed_stage": "reasoning_batch",
        "retryable": True,
    }
    assert set(type(public).model_fields) == {
        "error_id",
        "provider",
        "model",
        "code",
        "failed_stage",
        "retryable",
    }


def test_request_rejects_extra_secret_fields_and_non_strict_schema() -> None:
    payload = request().model_dump(mode="python")
    payload["api_key"] = "private"
    with pytest.raises(ValidationError):
        WaterbodyModelRequest.model_validate(payload)

    with pytest.raises(ValidationError):
        WaterbodyJsonSchema(
            name="reasoning_result",
            schema_text='{"type":"object"}',
            strict=False,
        )
