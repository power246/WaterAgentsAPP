from __future__ import annotations

import json
from typing import Annotated, Literal, Self, TypeAlias
from uuid import UUID

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    StrictBool,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from app.domain import (
    DomainEnum,
    DomainModel,
    ItemReference,
    ModelSelection,
    NonEmptyString,
)
from app.domain.common import UtcDatetime


def _require_json_string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("JSON object text must be a non-empty string")
    return value


def _require_non_empty_text(value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise ValueError("text must not be empty")
    return value


def _require_non_blank_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("text must not be blank")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON objects must not contain duplicate keys")
        result[key] = value
    return result


def _reject_non_finite_json_number(_: str) -> None:
    raise ValueError("JSON numbers must be finite")


_SECRET_FIELD_NAMES = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "credential",
        "credentials",
        "password",
        "refresh_token",
        "secret",
    }
)


def _reject_secret_fields(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.strip().lower() in _SECRET_FIELD_NAMES:
                raise ValueError("JSON object must not contain credential fields")
            _reject_secret_fields(item)
    elif isinstance(value, list):
        for item in value:
            _reject_secret_fields(item)


def _validate_json_object_text(value: str) -> str:
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite_json_number,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("value must contain one valid JSON object") from error
    if not isinstance(parsed, dict):
        raise ValueError("value must contain one JSON object")
    _reject_secret_fields(parsed)
    return value


JsonObjectText: TypeAlias = Annotated[
    str,
    BeforeValidator(_require_json_string),
    AfterValidator(_validate_json_object_text),
]
NonEmptyText: TypeAlias = Annotated[str, BeforeValidator(_require_non_empty_text)]
NonBlankText: TypeAlias = Annotated[str, BeforeValidator(_require_non_blank_text)]
ModelItemName: TypeAlias = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
    ),
]
TokenCount: TypeAlias = Annotated[int, Field(strict=True, ge=0)]


class WaterbodyModelPhase(DomainEnum):
    EVIDENCE_PLANNING = "evidence_planning"
    REASONING_BATCH = "reasoning_batch"
    OVERALL_SYNTHESIS = "overall_synthesis"
    SCHEMA_REPAIR = "schema_repair"


class WaterbodyMessageRole(DomainEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class WaterbodyModelMessage(DomainModel):
    message_id: NonEmptyString
    role: WaterbodyMessageRole
    text: NonBlankText
    provider_call_id: NonEmptyString | None = None
    tool_name: ModelItemName | None = None

    @model_validator(mode="after")
    def validate_role_fields(self) -> Self:
        tool_fields = (self.provider_call_id, self.tool_name)
        if self.role is WaterbodyMessageRole.TOOL:
            if any(value is None for value in tool_fields):
                raise ValueError("tool messages require call and tool identity")
        elif any(value is not None for value in tool_fields):
            raise ValueError("only tool messages may contain tool identity")
        return self


class WaterbodyJsonSchema(DomainModel):
    name: ModelItemName
    schema_text: JsonObjectText
    strict: StrictBool = True

    @field_validator("schema_text", mode="after")
    @classmethod
    def canonicalize_schema(cls, value: str) -> str:
        return json.dumps(
            json.loads(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @field_validator("strict")
    @classmethod
    def require_strict_schema(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("waterbody model schemas must be strict")
        return value


class WaterbodyToolDescriptor(DomainModel):
    name: ModelItemName
    description: NonEmptyString
    input_schema: WaterbodyJsonSchema

    @model_validator(mode="after")
    def validate_schema_name(self) -> Self:
        if self.input_schema.name != self.name:
            raise ValueError("tool descriptor and input schema names must match")
        return self


class WaterbodyModelRequest(DomainModel):
    operation_id: UUID
    phase: WaterbodyModelPhase
    model: ModelSelection
    system_prompt: NonBlankText
    messages: tuple[WaterbodyModelMessage, ...]
    tool_descriptors: tuple[WaterbodyToolDescriptor, ...] = ()
    response_schema: WaterbodyJsonSchema
    prompt_policy_digest: NonEmptyString
    scope_digest: NonEmptyString
    evidence_bundle_ref: ItemReference
    batch_id: NonEmptyString | None = None
    deadline: UtcDatetime

    @field_validator("messages")
    @classmethod
    def validate_messages(
        cls,
        value: tuple[WaterbodyModelMessage, ...],
    ) -> tuple[WaterbodyModelMessage, ...]:
        if not value:
            raise ValueError("waterbody model request requires messages")
        identities = tuple(message.message_id for message in value)
        if len(identities) != len(set(identities)):
            raise ValueError("waterbody model message identities must be unique")
        return value

    @field_validator("tool_descriptors")
    @classmethod
    def validate_tools(
        cls,
        value: tuple[WaterbodyToolDescriptor, ...],
    ) -> tuple[WaterbodyToolDescriptor, ...]:
        names = tuple(descriptor.name for descriptor in value)
        if len(names) != len(set(names)):
            raise ValueError("waterbody model tool names must be unique")
        return value


class WaterbodyFinishReason(DomainEnum):
    COMPLETED = "completed"
    TRUNCATED = "truncated"
    INCOMPLETE = "incomplete"
    CONTENT_FILTERED = "content_filtered"
    REFUSED = "refused"
    CANCELLED = "cancelled"


class _WaterbodyModelEventBase(DomainModel):
    operation_id: UUID
    model: ModelSelection


class VisibleTextDelta(_WaterbodyModelEventBase):
    event_type: Literal["visible_text_delta"] = "visible_text_delta"
    message_id: NonEmptyString
    text: NonEmptyText


class ToolCallCompleted(_WaterbodyModelEventBase):
    event_type: Literal["tool_call_completed"] = "tool_call_completed"
    provider_call_id: NonEmptyString
    name: ModelItemName
    arguments: JsonObjectText


class StructuredOutputCompleted(_WaterbodyModelEventBase):
    event_type: Literal["structured_output_completed"] = "structured_output_completed"
    output_id: NonEmptyString
    value: JsonObjectText


class RefusalCompleted(_WaterbodyModelEventBase):
    event_type: Literal["refusal_completed"] = "refusal_completed"
    message_id: NonEmptyString
    text: NonBlankText


class UsageReported(_WaterbodyModelEventBase):
    event_type: Literal["usage_reported"] = "usage_reported"
    input_tokens: TokenCount | None = None
    output_tokens: TokenCount | None = None

    @model_validator(mode="after")
    def require_reported_value(self) -> Self:
        if self.input_tokens is None and self.output_tokens is None:
            raise ValueError("usage event must report at least one token count")
        return self


class Terminal(_WaterbodyModelEventBase):
    event_type: Literal["terminal"] = "terminal"
    provider_response_id: NonEmptyString
    finish_reason: WaterbodyFinishReason


WaterbodyModelContentEvent: TypeAlias = (
    VisibleTextDelta
    | ToolCallCompleted
    | StructuredOutputCompleted
    | RefusalCompleted
)
WaterbodyModelEvent: TypeAlias = Annotated[
    WaterbodyModelContentEvent | UsageReported | Terminal,
    Field(discriminator="event_type"),
]


class WaterbodyModelUsage(DomainModel):
    input_tokens: TokenCount | None = None
    output_tokens: TokenCount | None = None

    @model_validator(mode="after")
    def require_usage_value(self) -> Self:
        if self.input_tokens is None and self.output_tokens is None:
            raise ValueError("model usage must contain at least one token count")
        return self


class WaterbodyModelCompletedResponse(DomainModel):
    operation_id: UUID
    phase: WaterbodyModelPhase
    model: ModelSelection
    provider_response_id: NonEmptyString
    finish_reason: Literal[WaterbodyFinishReason.COMPLETED] = (
        WaterbodyFinishReason.COMPLETED
    )
    events: tuple[WaterbodyModelContentEvent, ...]
    usage: WaterbodyModelUsage | None = None


class WaterbodyModelErrorCode(DomainEnum):
    UNAVAILABLE = "waterbody_model_unavailable"
    TIMEOUT = "waterbody_model_timeout"
    CONTENT_FILTERED = "waterbody_model_content_filtered"
    REFUSED = "waterbody_model_refused"
    RESPONSE_INVALID = "waterbody_model_response_invalid"
    TRUNCATED = "waterbody_model_truncated"
    INCOMPLETE = "waterbody_model_incomplete"
    CANCELLED = "waterbody_model_cancelled"


class WaterbodyModelPublicError(DomainModel):
    error_id: UUID
    provider: NonEmptyString
    model: NonEmptyString
    code: WaterbodyModelErrorCode
    failed_stage: WaterbodyModelPhase
    retryable: StrictBool


_EVENT_ADAPTER = TypeAdapter(WaterbodyModelEvent)


def parse_waterbody_model_event(value: object) -> WaterbodyModelEvent:
    return _EVENT_ADAPTER.validate_python(value)
