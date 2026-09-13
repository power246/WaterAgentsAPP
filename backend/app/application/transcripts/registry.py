import json
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

from pydantic import TypeAdapter, ValidationError

from app.application.analysis_runs.models import (
    LifecycleEvent,
    PublicError,
    RunCompletedEvent,
    RunFailedEvent,
    SessionFailedEvent,
)
from app.application.transcripts.models import (
    AnalysisRequestV1,
    ContentVisibility,
    FixtureMessageV1,
    LateMessageOutputFact,
    LegacyLifecycleEventV1,
    MessageProjection,
    ParseStatus,
    ReadTranscriptItem,
    ReferenceResolution,
    ReferenceResolutionStatus,
    RegisteredContent,
    RuntimeFact,
    StoredTranscriptItem,
    TranscriptContentRef,
    TranscriptMetadata,
)
from app.application.transcripts.redaction import redact
from app.domain.attribution.analysis_detail import AnalysisDetail
from app.domain.common import JsonValue
from app.domain.domain_model import DomainModel


class UnsupportedTranscriptContentError(ValueError):
    pass


ReferenceExtractor = Callable[[object], tuple[UUID, ...]]
MetadataExtractor = Callable[[object], TranscriptMetadata]
VisibilityExtractor = Callable[[object], ContentVisibility]
MessageAdapter = Callable[[object], MessageProjection]
ErrorExtractor = Callable[[object], tuple[UUID, ...]]


def _no_references(body: object) -> tuple[UUID, ...]:
    del body
    return ()


def _default_metadata(body: object) -> TranscriptMetadata:
    del body
    return TranscriptMetadata()


def _auto_visibility(body: object) -> ContentVisibility:
    del body
    return ContentVisibility.AUTO


@dataclass(frozen=True, slots=True)
class ContentRegistration:
    content_type: str
    content_schema_version: int
    adapter: TypeAdapter
    references: ReferenceExtractor = _no_references
    metadata: MetadataExtractor = _default_metadata
    visibility: VisibilityExtractor = _auto_visibility
    message_adapter: MessageAdapter | None = None
    errors: ErrorExtractor = _no_references
    history_only_possible: bool = False
    writable: bool = True
    preserve_unset: bool = False

    @property
    def key(self) -> tuple[str, int]:
        return self.content_type, self.content_schema_version


class TranscriptRegistry:
    def __init__(self) -> None:
        self._registrations: dict[tuple[str, int], ContentRegistration] = {}

    @property
    def registered_keys(self) -> tuple[tuple[str, int], ...]:
        return tuple(self._registrations)

    @property
    def writable_keys(self) -> tuple[tuple[str, int], ...]:
        return tuple(
            key for key, registration in self._registrations.items()
            if registration.writable
        )

    def register(self, registration: ContentRegistration) -> None:
        if not registration.content_type.strip():
            raise ValueError("content_type must not be blank")
        if (
            type(registration.content_schema_version) is not int
            or registration.content_schema_version <= 0
        ):
            raise ValueError("content_schema_version must be positive")
        if registration.key in self._registrations:
            raise ValueError(f"duplicate transcript registration: {registration.key!r}")
        self._registrations[registration.key] = registration

    def copy_with(self, registration: ContentRegistration) -> "TranscriptRegistry":
        copied = TranscriptRegistry()
        copied._registrations.update(self._registrations)
        copied.register(registration)
        return copied

    def encode(
        self,
        content_type: str,
        content_schema_version: int,
        body: object,
        *,
        private_values: tuple[str, ...] = (),
    ) -> RegisteredContent:
        registration = self._registrations.get((content_type, content_schema_version))
        if registration is None or not registration.writable:
            raise UnsupportedTranscriptContentError(
                f"unsupported transcript write: {content_type} v{content_schema_version}"
            )
        validated = registration.adapter.validate_python(body)
        serialized = registration.adapter.dump_python(
            validated,
            mode="json",
            exclude_unset=registration.preserve_unset,
        )
        safe_value = redact(serialized, private_values)
        safe_body = json.dumps(
            safe_value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        safe_model = registration.adapter.validate_json(safe_body)
        references = registration.references(safe_model)
        if len(references) != len(set(references)):
            raise ValueError("registered reference extractor returned duplicates")
        return RegisteredContent(
            content_type=content_type,
            content_schema_version=content_schema_version,
            safe_body=safe_body,
            content_bytes=len(safe_body.encode("utf-8")),
            references=references,
            metadata=registration.metadata(safe_model),
            visibility=registration.visibility(safe_model),
        )

    def parse(
        self,
        item: StoredTranscriptItem,
        safe_body: str,
        *,
        error_id_factory: Callable[[], UUID] = uuid4,
    ) -> ReadTranscriptItem:
        registration = self._registrations.get(
            (item.content_type, item.content_schema_version)
        )
        content_ref = TranscriptContentRef(run_id=item.run_id, item_id=item.item_id)
        if registration is None:
            return ReadTranscriptItem(
                item=item,
                content_ref=content_ref,
                parse_status=ParseStatus.UNSUPPORTED,
                parse_error=PublicError(
                    error_id=error_id_factory(),
                    code="transcript_content_unsupported",
                    message="This transcript content type or version is not supported.",
                    failed_stage="transcript_read",
                    retryable=False,
                ),
            )
        if len(safe_body.encode("utf-8")) != item.content_bytes:
            return ReadTranscriptItem(
                item=item,
                content_ref=content_ref,
                parse_status=ParseStatus.CORRUPT,
                parse_error=PublicError(
                    error_id=error_id_factory(),
                    code="transcript_content_corrupt",
                    message="This transcript item contains invalid content.",
                    failed_stage="transcript_read",
                    retryable=False,
                ),
            )
        try:
            body = registration.adapter.validate_json(safe_body)
        except (ValidationError, ValueError, TypeError):
            return ReadTranscriptItem(
                item=item,
                content_ref=content_ref,
                parse_status=ParseStatus.CORRUPT,
                parse_error=PublicError(
                    error_id=error_id_factory(),
                    code="transcript_content_corrupt",
                    message="This transcript item contains invalid content.",
                    failed_stage="transcript_read",
                    retryable=False,
                ),
            )
        if not isinstance(body, DomainModel):
            raise TypeError("registered transcript adapters must return DomainModel values")
        return ReadTranscriptItem(
            item=item,
            content_ref=content_ref,
            parse_status=ParseStatus.OK,
            body=body,
        )

    def recheck(
        self,
        content: RegisteredContent,
        *,
        private_values: tuple[str, ...] = (),
    ) -> RegisteredContent:
        registration = self._registrations.get(
            (content.content_type, content.content_schema_version)
        )
        if registration is None or not registration.writable:
            raise UnsupportedTranscriptContentError(
                "stored transcript content is not writable by this registry"
            )
        body = registration.adapter.validate_json(content.safe_body)
        checked = self.encode(
            content.content_type,
            content.content_schema_version,
            body,
            private_values=private_values,
        )
        if checked.safe_body != content.safe_body:
            raise ValueError("persistence redaction differs from the readable live body")
        return checked

    def parse_many(
        self,
        records: tuple[tuple[StoredTranscriptItem, str], ...],
        *,
        error_id_factory: Callable[[], UUID] = uuid4,
    ) -> tuple[ReadTranscriptItem, ...]:
        return tuple(
            self.parse(item, body, error_id_factory=error_id_factory)
            for item, body in records
        )

    def message_projection(
        self,
        content_type: str,
        content_schema_version: int,
        safe_body: str,
    ) -> MessageProjection:
        registration = self._registrations.get((content_type, content_schema_version))
        if registration is None or registration.message_adapter is None:
            raise UnsupportedTranscriptContentError(
                f"transcript type is not a message: {content_type} v{content_schema_version}"
            )
        body = registration.adapter.validate_json(safe_body)
        return registration.message_adapter(body)

    def is_message_type(
        self,
        content_type: str,
        content_schema_version: int,
    ) -> bool:
        registration = self._registrations.get((content_type, content_schema_version))
        return registration is not None and registration.message_adapter is not None

    def error_ids(self, content: RegisteredContent) -> tuple[UUID, ...]:
        registration = self._registrations.get(
            (content.content_type, content.content_schema_version)
        )
        if registration is None:
            raise UnsupportedTranscriptContentError(
                "stored transcript content is not registered"
            )
        body = registration.adapter.validate_json(content.safe_body)
        return registration.errors(body)

    def public_body(self, item: ReadTranscriptItem) -> JsonValue:
        registration = self._registrations.get(
            (item.item.content_type, item.item.content_schema_version)
        )
        if registration is None or item.body is None:
            raise UnsupportedTranscriptContentError(
                "unparsed transcript content has no public typed body"
            )
        return registration.adapter.dump_python(
            item.body,
            mode="json",
            exclude_unset=registration.preserve_unset,
        )

    def references_from_read(self, item: ReadTranscriptItem) -> tuple[UUID, ...]:
        registration = self._registrations.get(
            (item.item.content_type, item.item.content_schema_version)
        )
        if registration is None or item.body is None:
            return ()
        return registration.references(item.body)

    def metadata_from_read(self, item: ReadTranscriptItem) -> TranscriptMetadata:
        registration = self._registrations.get(
            (item.item.content_type, item.item.content_schema_version)
        )
        if registration is None or item.body is None:
            return TranscriptMetadata(
                tags=item.item.tags,
                rag_eligible=item.item.rag_eligible,
            )
        derived = registration.metadata(item.body)
        return TranscriptMetadata(
            tags=item.item.tags,
            rag_eligible=item.item.rag_eligible,
            confidence=derived.confidence,
            provenance=derived.provenance,
        )

    def visibility_from_read(self, item: ReadTranscriptItem) -> ContentVisibility:
        registration = self._registrations.get(
            (item.item.content_type, item.item.content_schema_version)
        )
        if registration is None:
            return ContentVisibility.HISTORY_ONLY
        if item.body is None:
            return (
                ContentVisibility.HISTORY_ONLY
                if registration.history_only_possible
                else ContentVisibility.AUTO
            )
        return registration.visibility(item.body)

    def error_ids_from_read(self, item: ReadTranscriptItem) -> tuple[UUID, ...]:
        registration = self._registrations.get(
            (item.item.content_type, item.item.content_schema_version)
        )
        if registration is None or item.body is None:
            return ()
        return registration.errors(item.body)


def _runtime_references(body: object) -> tuple[UUID, ...]:
    return (body.original_item_id,) if isinstance(body, LateMessageOutputFact) else ()


def _runtime_visibility(body: object) -> ContentVisibility:
    return (
        ContentVisibility.HISTORY_ONLY
        if isinstance(body, LateMessageOutputFact)
        else ContentVisibility.AUTO
    )


def _lifecycle_errors(body: object) -> tuple[UUID, ...]:
    if isinstance(body, (RunFailedEvent, SessionFailedEvent)):
        return (body.payload.error.error_id,)
    if isinstance(body, RunCompletedEvent) and body.payload.warning is not None:
        return (body.payload.warning.error_id,)
    return ()


def _standalone_error(body: object) -> tuple[UUID, ...]:
    if not isinstance(body, PublicError):
        raise TypeError("error registration received another content type")
    return (body.error_id,)


def _fixture_message_adapter(body: object) -> MessageProjection:
    if not isinstance(body, FixtureMessageV1):
        raise TypeError("fixture message adapter received another content type")
    return MessageProjection(
        text=body.text,
        generation_status=body.generation_status,
        end_reason=body.end_reason,
    )


def fixture_message_registration() -> ContentRegistration:
    return ContentRegistration(
        content_type="fixture_message",
        content_schema_version=1,
        adapter=TypeAdapter(FixtureMessageV1),
        message_adapter=_fixture_message_adapter,
    )


def create_production_registry() -> TranscriptRegistry:
    registry = TranscriptRegistry()
    registry.register(ContentRegistration(
        content_type="analysis_request",
        content_schema_version=1,
        adapter=TypeAdapter(AnalysisRequestV1),
        preserve_unset=True,
    ))
    registry.register(ContentRegistration(
        content_type="lifecycle_event",
        content_schema_version=2,
        adapter=TypeAdapter(LifecycleEvent),
        errors=_lifecycle_errors,
    ))
    registry.register(ContentRegistration(
        content_type="error",
        content_schema_version=1,
        adapter=TypeAdapter(PublicError),
        errors=_standalone_error,
    ))
    registry.register(ContentRegistration(
        content_type="runtime_fact",
        content_schema_version=1,
        adapter=TypeAdapter(RuntimeFact),
        references=_runtime_references,
        visibility=_runtime_visibility,
        history_only_possible=True,
    ))
    registry.register(ContentRegistration(
        content_type="lifecycle_event",
        content_schema_version=1,
        adapter=TypeAdapter(LegacyLifecycleEventV1),
        writable=False,
    ))
    return registry


def extract_analysis_detail_references(detail: AnalysisDetail) -> tuple[UUID, ...]:
    original = (
        *detail.transcript_item_refs,
        *(reference for step in detail.steps for reference in step.transcript_item_refs),
    )
    references: list[UUID] = []
    for value in original:
        reference = UUID(value)
        if reference not in references:
            references.append(reference)
    return tuple(references)


def validate_references(
    source_run_id: UUID,
    reference_ids: tuple[UUID, ...],
    *,
    persisted: tuple[TranscriptContentRef, ...] = (),
    same_batch: tuple[TranscriptContentRef, ...] = (),
    generating: tuple[TranscriptContentRef, ...] = (),
) -> ReferenceResolution:
    known = {*persisted, *same_batch}
    pending = set(generating)
    waiting: list[TranscriptContentRef] = []
    for item_id in reference_ids:
        target = TranscriptContentRef(run_id=source_run_id, item_id=item_id)
        if target in known:
            continue
        if target in pending:
            waiting.append(target)
            continue
        foreign = next(
            (
                candidate for candidate in (*persisted, *same_batch, *generating)
                if candidate.item_id == item_id and candidate.run_id != source_run_id
            ),
            None,
        )
        if foreign is not None:
            raise ValueError("transcript references must remain in the same run")
        raise ValueError("transcript reference target does not exist")
    if waiting:
        return ReferenceResolution(
            status=ReferenceResolutionStatus.WAITING,
            waiting_for=tuple(waiting),
        )
    return ReferenceResolution(status=ReferenceResolutionStatus.READY)
