from datetime import date
from enum import Enum
from typing import Annotated, Literal, TypeAlias
from uuid import UUID

from pydantic import (
    AfterValidator,
    Field,
    SerializeAsAny,
    StrictBool,
    StrictFloat,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

from app.application.analysis_runs.models import (
    AnalysisConfigurationSnapshot,
    LifecycleEvent,
    PositiveSequence,
    PublicError,
)
from app.domain.common import NonEmptyString, UtcDatetime, ensure_unique_tuple
from app.domain.domain_model import DomainModel
from app.domain.hydrology.location import GeoPoint
from app.domain.memory import MemoryMode
from app.domain.model_catalog import ModelSelection
from app.domain.stable_validation_error import stable_validation_error
from app.domain.time.time_resolution_preference import (
    DEFAULT_TIME_RESOLUTION_PREFERENCE,
    TimeResolutionPreference,
)
from app.domain.time.time_resolution_preference_source import (
    TimeResolutionPreferenceSource,
)

DEFAULT_INLINE_CONTENT_BYTES = 64 * 1024
DEFAULT_PREVIEW_BYTES = 1024


def _reject_nul_character(value: str) -> str:
    if "\x00" in value:
        raise ValueError("query must not contain NUL characters")
    return value


AnalysisQuery: TypeAlias = Annotated[
    NonEmptyString,
    AfterValidator(_reject_nul_character),
]


class AnalysisRequestV1(DomainModel):
    query: AnalysisQuery
    target: "AnalysisTargetV1 | SkipJsonSchema[None]" = None
    date_range: "AnalysisDateRangeV1 | SkipJsonSchema[None]" = None
    time_resolution: TimeResolutionPreference = DEFAULT_TIME_RESOLUTION_PREFERENCE
    parser_model: ModelSelection
    waterbody_model: ModelSelection
    memory_mode: MemoryMode

    @field_validator("target", "date_range", "time_resolution", mode="before")
    @classmethod
    def reject_explicit_nulls(cls, value: object, info: ValidationInfo) -> object:
        if value is None:
            field_name = info.field_name
            error_type = {
                "target": "target_value_required",
                "date_range": "date_range_incomplete",
                "time_resolution": "invalid_time_resolution",
            }[field_name]
            raise stable_validation_error(
                error_type,
                f"{field_name} must be omitted rather than null",
            )
        return value

    def to_configuration_snapshot(
        self, analysis_timezone: str
    ) -> AnalysisConfigurationSnapshot:
        source = (
            TimeResolutionPreferenceSource.FRONTEND
            if "time_resolution" in self.model_fields_set
            else TimeResolutionPreferenceSource.DEFAULT
        )
        return AnalysisConfigurationSnapshot(
            analysis_timezone=analysis_timezone,
            time_resolution_preference=self.time_resolution,
            time_resolution_preference_source=source,
            parser_model=self.parser_model,
            waterbody_model=self.waterbody_model,
            memory_mode=self.memory_mode,
        )


class AnalysisTargetV1(DomainModel):
    comid: str | SkipJsonSchema[None] = None
    coordinates: GeoPoint | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_null_members(cls, value: object) -> object:
        if isinstance(value, dict):
            if "comid" in value and value["comid"] is None:
                raise stable_validation_error("invalid_comid", "comid must not be null")
            if "coordinates" in value and value["coordinates"] is None:
                raise stable_validation_error(
                    "invalid_coordinates", "coordinates must not be null"
                )
        return value

    @field_validator("comid", mode="before")
    @classmethod
    def validate_comid(cls, value: object) -> object:
        if not isinstance(value, str) or not value.isdecimal():
            raise stable_validation_error(
                "invalid_comid", "comid must be a positive decimal string"
            )
        if value.startswith("0"):
            raise stable_validation_error(
                "invalid_comid", "comid must be a positive decimal string"
            )
        return value

    @model_validator(mode="after")
    def require_value(self) -> "AnalysisTargetV1":
        if self.comid is None and self.coordinates is None:
            raise stable_validation_error(
                "target_value_required",
                "target must include comid or coordinates",
            )
        return self


class AnalysisDateRangeV1(DomainModel):
    start_date: date
    end_date: date

    @model_validator(mode="before")
    @classmethod
    def require_complete_range(cls, value: object) -> object:
        if not isinstance(value, dict) or any(
            field_name not in value or value[field_name] is None
            for field_name in ("start_date", "end_date")
        ):
            raise stable_validation_error(
                "date_range_incomplete",
                "start_date and end_date must be provided together",
            )
        return value

    @model_validator(mode="after")
    def validate_range(self) -> "AnalysisDateRangeV1":
        if self.start_date > self.end_date:
            raise stable_validation_error(
                "date_range_reversed", "start_date must not follow end_date"
            )
        return self


AnalysisRequestV1.model_rebuild()


class LegacySessionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    COMPLETED_WITH_PERSISTENCE_ERROR = "completed_with_persistence_error"


class LegacySessionCreatedPayloadV1(DomainModel):
    status: Literal[LegacySessionStatus.PENDING] = LegacySessionStatus.PENDING
    created_at: UtcDatetime
    configuration: AnalysisConfigurationSnapshot


class LegacySessionCompletedPayloadV1(DomainModel):
    status: Literal[LegacySessionStatus.COMPLETED] = LegacySessionStatus.COMPLETED
    completed_at: UtcDatetime


class LegacySessionFailedPayloadV1(DomainModel):
    status: Literal[LegacySessionStatus.FAILED] = LegacySessionStatus.FAILED
    failed_at: UtcDatetime
    error: PublicError


class LegacySessionCancelledPayloadV1(DomainModel):
    status: Literal[LegacySessionStatus.CANCELLED] = LegacySessionStatus.CANCELLED
    cancelled_at: UtcDatetime


class _LegacyLifecycleEventV1(DomainModel):
    schema_version: Literal[1] = 1
    session_id: UUID
    sequence: PositiveSequence
    occurred_at: UtcDatetime

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be integer 1")
        return value


class LegacySessionCreatedEventV1(_LegacyLifecycleEventV1):
    sequence: PositiveSequence = Field(default=1, le=1)
    type: Literal["session_created"] = "session_created"
    payload: LegacySessionCreatedPayloadV1


class LegacySessionCompletedEventV1(_LegacyLifecycleEventV1):
    type: Literal["session_completed"] = "session_completed"
    payload: LegacySessionCompletedPayloadV1


class LegacySessionFailedEventV1(_LegacyLifecycleEventV1):
    type: Literal["session_failed"] = "session_failed"
    payload: LegacySessionFailedPayloadV1


class LegacySessionCancelledEventV1(_LegacyLifecycleEventV1):
    type: Literal["session_cancelled"] = "session_cancelled"
    payload: LegacySessionCancelledPayloadV1


LegacyLifecycleEventV1: TypeAlias = Annotated[
    LegacySessionCreatedEventV1
    | LegacySessionCompletedEventV1
    | LegacySessionFailedEventV1
    | LegacySessionCancelledEventV1,
    Field(discriminator="type"),
]


class WorkflowQuiescedFact(DomainModel):
    fact: Literal["workflow_quiesced"] = "workflow_quiesced"


class PersistenceRecoveredFact(DomainModel):
    fact: Literal["persistence_recovered"] = "persistence_recovered"
    error_id: UUID


class SuppressedTranscriptItemFact(DomainModel):
    fact: Literal["suppressed_transcript_item"] = "suppressed_transcript_item"
    reason: Literal["cancelled", "error"]


class MessageEndReason(str, Enum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"
    TRUNCATED = "truncated"


class LateMessageOutputFact(DomainModel):
    fact: Literal["late_message_output"] = "late_message_output"
    original_item_id: UUID
    text: str
    end_reason: MessageEndReason

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if value == "":
            raise ValueError("late message output must not be empty")
        return value


RuntimeFact: TypeAlias = Annotated[
    WorkflowQuiescedFact
    | PersistenceRecoveredFact
    | SuppressedTranscriptItemFact
    | LateMessageOutputFact,
    Field(discriminator="fact"),
]


class GenerationStatus(str, Enum):
    GENERATING = "generating"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"


class ContentVisibility(str, Enum):
    AUTO = "auto"
    HISTORY_ONLY = "history_only"


class TranscriptMetadata(DomainModel):
    tags: tuple[NonEmptyString, ...] = ()
    rag_eligible: StrictBool = False
    confidence: Annotated[StrictFloat, Field(ge=0, le=1)] | None = None
    provenance: tuple[NonEmptyString, ...] | None = None

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return ensure_unique_tuple(value, field_name="tags")


class TranscriptContentRef(DomainModel):
    run_id: UUID
    item_id: UUID


class RegisteredContent(DomainModel):
    content_type: NonEmptyString
    content_schema_version: int = Field(strict=True, gt=0)
    safe_body: str
    content_bytes: int = Field(strict=True, ge=0)
    references: tuple[UUID, ...] = ()
    metadata: TranscriptMetadata = TranscriptMetadata()
    visibility: ContentVisibility = ContentVisibility.AUTO

    @model_validator(mode="after")
    def validate_encoded_size(self) -> "RegisteredContent":
        if len(self.safe_body.encode("utf-8")) != self.content_bytes:
            raise ValueError("content_bytes must match safe_body UTF-8 size")
        if len(self.references) != len(set(self.references)):
            raise ValueError("references must not contain duplicates")
        return self


class TranscriptItem(DomainModel):
    item_id: UUID
    run_id: UUID
    session_id: UUID | None = None
    transcript_sequence: PositiveSequence
    created_at: UtcDatetime
    request_id: UUID | None = None
    operation_id: UUID | None = None
    content: RegisteredContent

    @property
    def content_ref(self) -> TranscriptContentRef:
        return TranscriptContentRef(run_id=self.run_id, item_id=self.item_id)


class ContentLocation(DomainModel):
    inline_body: str | None = None
    preview_head: str | None = None
    preview_tail: str | None = None

    @model_validator(mode="after")
    def validate_location(self) -> "ContentLocation":
        inline = self.inline_body is not None
        previews = self.preview_head is not None and self.preview_tail is not None
        if inline == previews:
            raise ValueError("content must be inline or have both previews")
        return self


class StoredTranscriptItem(DomainModel):
    item_id: UUID
    run_id: UUID
    session_id: UUID | None = None
    transcript_sequence: PositiveSequence
    created_at: UtcDatetime
    content_type: NonEmptyString
    content_schema_version: int = Field(strict=True, gt=0)
    request_id: UUID | None = None
    operation_id: UUID | None = None
    tags: tuple[NonEmptyString, ...] = ()
    rag_eligible: StrictBool = False
    inline_body: str | None = None
    preview_head: str | None = None
    preview_tail: str | None = None
    content_bytes: int = Field(strict=True, ge=0)

    @model_validator(mode="after")
    def validate_body_location(self) -> "StoredTranscriptItem":
        ContentLocation(
            inline_body=self.inline_body,
            preview_head=self.preview_head,
            preview_tail=self.preview_tail,
        )
        if (
            self.inline_body is not None
            and len(self.inline_body.encode("utf-8")) != self.content_bytes
        ):
            raise ValueError("inline content_bytes must match UTF-8 body size")
        return self


class StoredTranscriptContent(DomainModel):
    item_id: UUID
    body: str
    created_at: UtcDatetime


class TranscriptStorageBatch(DomainModel):
    items: tuple[StoredTranscriptItem, ...] = ()
    contents: tuple[StoredTranscriptContent, ...] = ()

    @model_validator(mode="after")
    def validate_storage(self) -> "TranscriptStorageBatch":
        item_ids = [item.item_id for item in self.items]
        content_ids = [content.item_id for content in self.contents]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("duplicate item_id in transcript storage batch")
        if len(content_ids) != len(set(content_ids)):
            raise ValueError("duplicate item_id in transcript contents")
        items = {item.item_id: item for item in self.items}
        contents = {content.item_id: content for content in self.contents}
        if set(contents) - set(items):
            raise ValueError("transcript content requires its item in the same batch")
        for item in self.items:
            content = contents.get(item.item_id)
            if item.inline_body is not None:
                if content is not None:
                    raise ValueError("inline item must not have transcript content")
                continue
            if content is None:
                raise ValueError("external item requires transcript content in the same batch")
            if content.created_at != item.created_at:
                raise ValueError("external content created_at must match its item")
            if len(content.body.encode("utf-8")) != item.content_bytes:
                raise ValueError("external content_bytes must match UTF-8 body size")
        return self


class ParseStatus(str, Enum):
    OK = "ok"
    UNSUPPORTED = "unsupported"
    CORRUPT = "corrupt"


class ReadTranscriptItem(DomainModel):
    item: StoredTranscriptItem
    content_ref: TranscriptContentRef
    parse_status: ParseStatus
    body: SerializeAsAny[DomainModel] | None = None
    parse_error: PublicError | None = None

    @model_validator(mode="after")
    def validate_parse_result(self) -> "ReadTranscriptItem":
        if self.content_ref != TranscriptContentRef(
            run_id=self.item.run_id,
            item_id=self.item.item_id,
        ):
            raise ValueError("content_ref must identify the transcript item")
        is_ok = self.parse_status is ParseStatus.OK
        if is_ok != (self.body is not None):
            raise ValueError("only ok transcript items contain a parsed body")
        if is_ok == (self.parse_error is not None):
            raise ValueError("unsupported and corrupt items require a parse error")
        return self


class MessageProjection(DomainModel):
    text: str
    generation_status: GenerationStatus
    end_reason: MessageEndReason | None = None


class ReferenceResolutionStatus(str, Enum):
    READY = "ready"
    WAITING = "waiting"


class ReferenceResolution(DomainModel):
    status: ReferenceResolutionStatus
    waiting_for: tuple[TranscriptContentRef, ...] = ()

    @model_validator(mode="after")
    def validate_waiting_targets(self) -> "ReferenceResolution":
        is_waiting = self.status is ReferenceResolutionStatus.WAITING
        if is_waiting != bool(self.waiting_for):
            raise ValueError("waiting resolution must include pending targets")
        return self


TypedTranscriptBody: TypeAlias = (
    AnalysisRequestV1
    | LifecycleEvent
    | LegacyLifecycleEventV1
    | PublicError
    | RuntimeFact
)
