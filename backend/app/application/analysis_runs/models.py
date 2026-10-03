from typing import Annotated, Literal, TypeAlias
from uuid import UUID

from pydantic import (
    Discriminator,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    Tag,
    field_validator,
    model_validator,
)

from app.domain.common import NonEmptyString, UtcDatetime
from app.domain.common import DomainFloat
from app.domain.domain_enum import DomainEnum
from app.domain.domain_model import DomainModel
from app.domain.inference import AssessmentAvailability, RiskLevel
from app.domain.memory import MemoryMode
from app.domain.model_catalog import ModelSelection
from app.domain.time.time_resolution_preference import TimeResolutionPreference
from app.domain.time.time_resolution_preference_source import (
    TimeResolutionPreferenceSource,
)
from app.domain.time.timezone import get_iana_timezone

PositiveSequence: TypeAlias = Annotated[int, Field(strict=True, gt=0)]
StableCode: TypeAlias = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$"),
]


class RunStatus(DomainEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    COMPLETED_WITH_PERSISTENCE_ERROR = "completed_with_persistence_error"


class AnalysisPhase(DomainEnum):
    QUEUED = "queued"
    QUERY_UNDERSTANDING = "query_understanding"
    TOPOLOGY_BUILDING = "topology_building"
    TOPOLOGY_READY = "topology_ready"
    WATERBODY_EXECUTION = "waterbody_execution"
    RUN_SUMMARIZATION = "run_summarization"
    TERMINAL = "terminal"


class AgentSessionStatus(DomainEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentRole(DomainEnum):
    QUERY_PARSER = "query_parser"
    WATERBODY = "waterbody"
    RUN_SUMMARIZER = "run_summarizer"


class AnalysisConfigurationSnapshot(DomainModel):
    analysis_timezone: NonEmptyString
    time_resolution_preference: TimeResolutionPreference
    time_resolution_preference_source: TimeResolutionPreferenceSource
    parser_model: ModelSelection
    waterbody_model: ModelSelection
    memory_mode: MemoryMode

    @field_validator("analysis_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        get_iana_timezone(value)
        return value

    @model_validator(mode="after")
    def validate_resolution_preference_source(self) -> "AnalysisConfigurationSnapshot":
        if (
            self.time_resolution_preference_source
            is TimeResolutionPreferenceSource.DEFAULT
            and self.time_resolution_preference
            is not TimeResolutionPreference.TWELVE_HOURS
        ):
            raise ValueError("default time resolution preference must be 12h")
        return self


class AnalysisRunSnapshot(DomainModel):
    run_id: UUID
    status: RunStatus
    created_at: UtcDatetime
    updated_at: UtcDatetime
    configuration: AnalysisConfigurationSnapshot

    @model_validator(mode="after")
    def validate_timestamps(self) -> "AnalysisRunSnapshot":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        return self


class SessionResultReference(DomainModel):
    run_id: UUID
    item_id: UUID


class WaterbodySessionResultSummary(DomainModel):
    result_kind: Literal["waterbody"] = "waterbody"
    result_id: NonEmptyString
    target_comid: NonEmptyString
    risk_level: RiskLevel
    availability: AssessmentAvailability
    overall_confidence: Annotated[DomainFloat, Field(ge=0, le=1)]


class RunSummarySessionResultSummary(DomainModel):
    result_kind: Literal["run_summary"] = "run_summary"
    highlight_count: Annotated[StrictInt, Field(ge=0)]
    language: NonEmptyString


def _session_result_kind(value: object) -> str | None:
    if isinstance(value, WaterbodySessionResultSummary):
        return "waterbody"
    if isinstance(value, RunSummarySessionResultSummary):
        return "run_summary"
    if isinstance(value, dict):
        explicit = value.get("result_kind")
        if explicit is not None:
            return str(explicit)
        if "result_id" in value or "target_comid" in value:
            return "waterbody"
        if "highlight_count" in value or "language" in value:
            return "run_summary"
    return None


SessionResultSummary: TypeAlias = Annotated[
    Annotated[WaterbodySessionResultSummary, Tag("waterbody")]
    | Annotated[RunSummarySessionResultSummary, Tag("run_summary")],
    Discriminator(_session_result_kind),
]


class SessionSummary(DomainModel):
    session_id: UUID
    agent_role: AgentRole
    target_comid: NonEmptyString | None = None
    status: AgentSessionStatus
    created_at: UtcDatetime
    updated_at: UtcDatetime
    result_ref: SessionResultReference | None = None
    result_summary: SessionResultSummary | None = None

    @model_validator(mode="after")
    def validate_role_and_timestamps(self) -> "SessionSummary":
        requires_comid = self.agent_role is AgentRole.WATERBODY
        if requires_comid != (self.target_comid is not None):
            raise ValueError("only waterbody sessions require target_comid")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        result_pair = (self.result_ref is not None, self.result_summary is not None)
        if result_pair[0] != result_pair[1]:
            raise ValueError("result_ref and result_summary must appear together")
        if self.agent_role is AgentRole.QUERY_PARSER and any(result_pair):
            raise ValueError("query_parser sessions must not have a result")
        if self.agent_role is not AgentRole.QUERY_PARSER:
            completed = self.status is AgentSessionStatus.COMPLETED
            if completed != all(result_pair):
                raise ValueError("completed result sessions require a result")
        if self.agent_role is AgentRole.WATERBODY and all(result_pair):
            if not isinstance(self.result_summary, WaterbodySessionResultSummary):
                raise ValueError("waterbody sessions require a waterbody result")
            if self.result_summary.target_comid != self.target_comid:
                raise ValueError("waterbody result summary must match the session")
        if self.agent_role is AgentRole.RUN_SUMMARIZER and all(result_pair):
            if not isinstance(self.result_summary, RunSummarySessionResultSummary):
                raise ValueError("run_summarizer sessions require a run summary result")
        return self


class PublicError(DomainModel):
    error_id: UUID
    code: StableCode
    message: NonEmptyString
    failed_stage: StableCode | None = None
    retryable: StrictBool


class CorrelationContext(DomainModel):
    request_id: UUID | None = None
    run_id: UUID | None = None
    session_id: UUID | None = None
    operation_id: UUID | None = None
    error_id: UUID | None = None
    event_sequence: PositiveSequence | None = None


class TranscriptItemIdentity(DomainModel):
    item_id: UUID
    run_id: UUID
    session_id: UUID | None = None
    transcript_sequence: PositiveSequence
    created_at: UtcDatetime
    request_id: UUID | None = None
    operation_id: UUID | None = None


class OperationIdentity(DomainModel):
    operation_id: UUID
    run_id: UUID
    session_id: UUID


class RunStateMutation(DomainModel):
    mutation_id: UUID
    previous_status: RunStatus | None
    snapshot: AnalysisRunSnapshot
    transcript_item: TranscriptItemIdentity | None = None


class SessionStateMutation(DomainModel):
    mutation_id: UUID
    run_id: UUID
    previous_status: AgentSessionStatus | None
    summary: SessionSummary
    transcript_item: TranscriptItemIdentity | None = None


StateMutation: TypeAlias = RunStateMutation | SessionStateMutation


class LifecycleEventType(DomainEnum):
    RUN_CREATED = "run_created"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"
    SESSION_CREATED = "session_created"
    SESSION_COMPLETED = "session_completed"
    SESSION_FAILED = "session_failed"
    SESSION_CANCELLED = "session_cancelled"


class RunCreatedPayload(DomainModel):
    status: Literal[RunStatus.PENDING] = RunStatus.PENDING
    created_at: UtcDatetime
    configuration: AnalysisConfigurationSnapshot


class RunCompletedPayload(DomainModel):
    status: Literal[
        RunStatus.COMPLETED,
        RunStatus.COMPLETED_WITH_PERSISTENCE_ERROR,
    ]
    completed_at: UtcDatetime
    warning: PublicError | None = None

    @model_validator(mode="after")
    def validate_warning(self) -> "RunCompletedPayload":
        has_persistence_error = (
            self.status is RunStatus.COMPLETED_WITH_PERSISTENCE_ERROR
        )
        if has_persistence_error != (self.warning is not None):
            raise ValueError("warning must match persistence-error completion status")
        return self


class RunFailedPayload(DomainModel):
    status: Literal[RunStatus.FAILED] = RunStatus.FAILED
    failed_at: UtcDatetime
    error: PublicError


class RunCancelledPayload(DomainModel):
    status: Literal[RunStatus.CANCELLED] = RunStatus.CANCELLED
    cancelled_at: UtcDatetime


class SessionCreatedPayload(DomainModel):
    status: Literal[AgentSessionStatus.PENDING] = AgentSessionStatus.PENDING
    created_at: UtcDatetime


class SessionCompletedPayload(DomainModel):
    status: Literal[AgentSessionStatus.COMPLETED] = AgentSessionStatus.COMPLETED
    completed_at: UtcDatetime
    result_ref: SessionResultReference | None = None
    result_summary: SessionResultSummary | None = None

    @model_validator(mode="after")
    def validate_result_pair(self) -> "SessionCompletedPayload":
        if (self.result_ref is None) != (self.result_summary is None):
            raise ValueError("result_ref and result_summary must appear together")
        return self


class SessionFailedPayload(DomainModel):
    status: Literal[AgentSessionStatus.FAILED] = AgentSessionStatus.FAILED
    failed_at: UtcDatetime
    error: PublicError


class SessionCancelledPayload(DomainModel):
    status: Literal[AgentSessionStatus.CANCELLED] = AgentSessionStatus.CANCELLED
    cancelled_at: UtcDatetime


class _LifecycleEventEnvelope(DomainModel):
    schema_version: Literal[2] = 2
    run_id: UUID
    sequence: PositiveSequence
    occurred_at: UtcDatetime

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be integer 2")
        return value


class _RunLifecycleEventEnvelope(_LifecycleEventEnvelope):
    session_id: None = None


class _SessionLifecycleEventEnvelope(_LifecycleEventEnvelope):
    session_id: UUID


class RunCreatedEvent(_RunLifecycleEventEnvelope):
    sequence: PositiveSequence = Field(default=1, le=1)
    type: Literal[LifecycleEventType.RUN_CREATED] = LifecycleEventType.RUN_CREATED
    payload: RunCreatedPayload


class RunCompletedEvent(_RunLifecycleEventEnvelope):
    type: Literal[LifecycleEventType.RUN_COMPLETED] = LifecycleEventType.RUN_COMPLETED
    payload: RunCompletedPayload


class RunFailedEvent(_RunLifecycleEventEnvelope):
    type: Literal[LifecycleEventType.RUN_FAILED] = LifecycleEventType.RUN_FAILED
    payload: RunFailedPayload


class RunCancelledEvent(_RunLifecycleEventEnvelope):
    type: Literal[LifecycleEventType.RUN_CANCELLED] = LifecycleEventType.RUN_CANCELLED
    payload: RunCancelledPayload


class SessionCreatedEvent(_SessionLifecycleEventEnvelope):
    sequence: PositiveSequence = Field(default=1, le=1)
    type: Literal[LifecycleEventType.SESSION_CREATED] = (
        LifecycleEventType.SESSION_CREATED
    )
    payload: SessionCreatedPayload


class SessionCompletedEvent(_SessionLifecycleEventEnvelope):
    type: Literal[LifecycleEventType.SESSION_COMPLETED] = (
        LifecycleEventType.SESSION_COMPLETED
    )
    payload: SessionCompletedPayload

    @model_validator(mode="after")
    def validate_result_run(self) -> "SessionCompletedEvent":
        if (
            self.payload.result_ref is not None
            and self.payload.result_ref.run_id != self.run_id
        ):
            raise ValueError("session result reference must remain in the run")
        return self


class SessionFailedEvent(_SessionLifecycleEventEnvelope):
    type: Literal[LifecycleEventType.SESSION_FAILED] = LifecycleEventType.SESSION_FAILED
    payload: SessionFailedPayload


class SessionCancelledEvent(_SessionLifecycleEventEnvelope):
    type: Literal[LifecycleEventType.SESSION_CANCELLED] = (
        LifecycleEventType.SESSION_CANCELLED
    )
    payload: SessionCancelledPayload


RunLifecycleEvent: TypeAlias = Annotated[
    RunCreatedEvent | RunCompletedEvent | RunFailedEvent | RunCancelledEvent,
    Field(discriminator="type"),
]
SessionLifecycleEvent: TypeAlias = Annotated[
    SessionCreatedEvent
    | SessionCompletedEvent
    | SessionFailedEvent
    | SessionCancelledEvent,
    Field(discriminator="type"),
]
LifecycleEvent: TypeAlias = Annotated[
    RunCreatedEvent
    | RunCompletedEvent
    | RunFailedEvent
    | RunCancelledEvent
    | SessionCreatedEvent
    | SessionCompletedEvent
    | SessionFailedEvent
    | SessionCancelledEvent,
    Field(discriminator="type"),
]
