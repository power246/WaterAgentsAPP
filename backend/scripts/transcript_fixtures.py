from pydantic import TypeAdapter

from app.application.transcripts.models import (
    GenerationStatus,
    MessageEndReason,
    MessageProjection,
)
from app.application.transcripts.registry import ContentRegistration
from app.domain.domain_model import DomainModel


class FixtureMessageV1(DomainModel):
    text: str
    generation_status: GenerationStatus
    end_reason: MessageEndReason | None = None


def _message_adapter(body: object) -> MessageProjection:
    if not isinstance(body, FixtureMessageV1):
        raise TypeError("fixture message adapter received another content type")
    return MessageProjection(
        text=body.text,
        generation_status=body.generation_status,
        end_reason=body.end_reason,
    )


def _message_finalizer(
    text: str,
    generation_status: GenerationStatus,
    end_reason: MessageEndReason | None,
) -> FixtureMessageV1:
    return FixtureMessageV1(
        text=text,
        generation_status=generation_status,
        end_reason=end_reason,
    )


def fixture_message_registration() -> ContentRegistration:
    return ContentRegistration(
        content_type="fixture_message",
        content_schema_version=1,
        adapter=TypeAdapter(FixtureMessageV1),
        message_adapter=_message_adapter,
        message_finalizer=_message_finalizer,
    )
