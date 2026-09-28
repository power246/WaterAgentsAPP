from __future__ import annotations

from typing import Literal, Self

from pydantic import field_validator, model_validator

from app.application.transcripts.models import (
    GenerationStatus,
    MessageEndReason,
    MessageProjection,
)
from app.domain import DomainEnum, DomainModel, ModelSelection


class ParserMessageRole(DomainEnum):
    SYSTEM = "system"
    USER = "user"


class ParserPromptMessage(DomainModel):
    role: ParserMessageRole
    text: str

    @field_validator("text")
    @classmethod
    def require_text(cls, value: str) -> str:
        if not value:
            raise ValueError("parser prompt message text must not be empty")
        return value


class QueryParserInputV1(DomainModel):
    prompt_template_version: Literal[1] = 1
    response_schema_version: Literal[1] = 1
    model: ModelSelection
    messages: tuple[ParserPromptMessage, ...]
    tools: tuple[str, ...] = ()

    @field_validator("prompt_template_version", "response_schema_version", mode="before")
    @classmethod
    def require_integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("parser schema versions must be integer 1")
        return value

    @model_validator(mode="after")
    def validate_provider_request(self) -> Self:
        if tuple(message.role for message in self.messages) != (
            ParserMessageRole.SYSTEM,
            ParserMessageRole.USER,
        ):
            raise ValueError("parser request must contain one system and one user message")
        if self.tools:
            raise ValueError("query parser tools must be empty")
        return self


class ParserFinishReason(DomainEnum):
    STOP = "stop"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    TOOL_CALL = "tool_call"


class QueryParserResponse(DomainModel):
    text: str
    finish_reason: ParserFinishReason


class QueryParserMessageV1(DomainModel):
    text: str
    generation_status: GenerationStatus
    end_reason: MessageEndReason | None = None

    @model_validator(mode="after")
    def validate_generation_state(self) -> Self:
        if self.generation_status is GenerationStatus.GENERATING:
            if self.end_reason is not None:
                raise ValueError("generating parser message cannot have an end reason")
        elif self.end_reason is None:
            raise ValueError("final parser message requires an end reason")
        if (
            self.generation_status is GenerationStatus.COMPLETED
            and self.end_reason is not MessageEndReason.COMPLETED
        ):
            raise ValueError("completed parser message requires completed end reason")
        if (
            self.generation_status is GenerationStatus.INCOMPLETE
            and self.end_reason is MessageEndReason.COMPLETED
        ):
            raise ValueError("incomplete parser message cannot have completed end reason")
        return self


def query_parser_message_projection(body: object) -> MessageProjection:
    if not isinstance(body, QueryParserMessageV1):
        raise TypeError("query parser message adapter received another content type")
    return MessageProjection(
        text=body.text,
        generation_status=body.generation_status,
        end_reason=body.end_reason,
    )


def finalize_query_parser_message(
    text: str,
    generation_status: GenerationStatus,
    end_reason: MessageEndReason | None,
) -> QueryParserMessageV1:
    return QueryParserMessageV1(
        text=text,
        generation_status=generation_status,
        end_reason=end_reason,
    )
