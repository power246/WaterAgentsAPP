from typing import Annotated, Literal, TypeAlias
from uuid import UUID

from pydantic import Field, StrictBool, StringConstraints, field_validator

from app.domain.common import NonEmptyString, UtcDatetime
from app.domain.domain_enum import DomainEnum
from app.domain.domain_model import DomainModel
from app.domain.time.time_resolution import TimeResolution
from app.domain.time.time_resolution_source import TimeResolutionSource
from app.domain.time.timezone import get_iana_timezone

PositiveSequence: TypeAlias = Annotated[int, Field(strict=True, gt=0)]
StableCode: TypeAlias = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$"),
]


class SessionStatus(DomainEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    COMPLETED_WITH_PERSISTENCE_ERROR = "completed_with_persistence_error"


class AnalysisConfigurationSnapshot(DomainModel):
    analysis_timezone: NonEmptyString
    time_resolution: TimeResolution
    time_resolution_source: TimeResolutionSource

    @field_validator("analysis_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        get_iana_timezone(value)
        return value


class AnalysisSessionSnapshot(DomainModel):
    session_id: UUID
    status: SessionStatus
    created_at: UtcDatetime
    updated_at: UtcDatetime
    configuration: AnalysisConfigurationSnapshot


class PublicError(DomainModel):
    error_id: UUID
    code: StableCode
    message: NonEmptyString
    failed_stage: StableCode | None = None
    retryable: StrictBool


class CorrelationContext(DomainModel):
    request_id: UUID | None = None
    session_id: UUID | None = None
    error_id: UUID | None = None
    event_sequence: PositiveSequence | None = None


class LifecycleEventType(DomainEnum):
    SESSION_CREATED = "session_created"
    SESSION_COMPLETED = "session_completed"
    SESSION_FAILED = "session_failed"
    SESSION_CANCELLED = "session_cancelled"


class SessionCreatedPayload(DomainModel):
    status: Literal[SessionStatus.PENDING] = SessionStatus.PENDING
    created_at: UtcDatetime
    configuration: AnalysisConfigurationSnapshot


class SessionCompletedPayload(DomainModel):
    status: Literal[SessionStatus.COMPLETED] = SessionStatus.COMPLETED
    completed_at: UtcDatetime


class SessionFailedPayload(DomainModel):
    status: Literal[SessionStatus.FAILED] = SessionStatus.FAILED
    failed_at: UtcDatetime
    error: PublicError


class SessionCancelledPayload(DomainModel):
    status: Literal[SessionStatus.CANCELLED] = SessionStatus.CANCELLED
    cancelled_at: UtcDatetime


class _LifecycleEventEnvelope(DomainModel):
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


class SessionCreatedEvent(_LifecycleEventEnvelope):
    sequence: PositiveSequence = Field(default=1, le=1)
    type: Literal[LifecycleEventType.SESSION_CREATED] = (
        LifecycleEventType.SESSION_CREATED
    )
    payload: SessionCreatedPayload


class SessionCompletedEvent(_LifecycleEventEnvelope):
    type: Literal[LifecycleEventType.SESSION_COMPLETED] = (
        LifecycleEventType.SESSION_COMPLETED
    )
    payload: SessionCompletedPayload


class SessionFailedEvent(_LifecycleEventEnvelope):
    type: Literal[LifecycleEventType.SESSION_FAILED] = LifecycleEventType.SESSION_FAILED
    payload: SessionFailedPayload


class SessionCancelledEvent(_LifecycleEventEnvelope):
    type: Literal[LifecycleEventType.SESSION_CANCELLED] = (
        LifecycleEventType.SESSION_CANCELLED
    )
    payload: SessionCancelledPayload


LifecycleEvent: TypeAlias = Annotated[
    SessionCreatedEvent
    | SessionCompletedEvent
    | SessionFailedEvent
    | SessionCancelledEvent,
    Field(discriminator="type"),
]
