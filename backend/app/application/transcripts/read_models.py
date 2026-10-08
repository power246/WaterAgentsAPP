from datetime import date
from typing import Annotated, Literal, TypeAlias
from uuid import UUID

from pydantic import Field, StrictBool, StrictFloat, model_validator

from app.application.analysis_runs.models import (
    AgentRole,
    AgentSessionStatus,
    AnalysisConfigurationSnapshot,
    AnalysisRunSnapshot,
    PublicError,
    RunStatus,
    SessionSummary,
    SessionResultReference,
    SessionResultSummary,
)
from app.application.analysis_runs.persistence import PersistenceInfo
from app.application.analysis_runs.topology_result_models import (
    TopologyRunResultSummary,
)
from app.application.query_understanding.context_models import ParameterIntentSource
from app.application.transcripts.models import (
    ParseStatus,
    ReadTranscriptItem,
    StoredTranscriptContent,
    StoredTranscriptItem,
    TranscriptContentRef,
    TranscriptItem,
)
from app.domain import InferenceMode, TimeResolution
from app.domain.common import DomainFloat, JsonValue, NonEmptyString, UtcDatetime
from app.domain.domain_enum import DomainEnum
from app.domain.domain_model import DomainModel


class ReadView(DomainEnum):
    AUTO = "auto"
    HISTORY = "history"


class ReadSource(DomainEnum):
    LIVE = "live"
    POSTGRESQL = "postgresql"


class ReadIssue(DomainModel):
    item_id: UUID
    error: PublicError


class ReadSessionSummary(DomainModel):
    session_id: UUID
    agent_role: AgentRole
    target_comid: NonEmptyString | None = None
    status: AgentSessionStatus
    created_at: UtcDatetime
    updated_at: UtcDatetime
    result_ref: SessionResultReference | None = None
    result_summary: SessionResultSummary | None = None
    terminal_corrupt: StrictBool = Field(default=False, exclude=True)

    @model_validator(mode="after")
    def validate_read_state(self) -> "ReadSessionSummary":
        if (self.agent_role is AgentRole.WATERBODY) != (self.target_comid is not None):
            raise ValueError("only waterbody sessions require target_comid")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        has_result = self.result_ref is not None and self.result_summary is not None
        if (self.result_ref is None) != (self.result_summary is None):
            raise ValueError("result_ref and result_summary must appear together")
        completed_result = (
            self.status is AgentSessionStatus.COMPLETED
            and self.agent_role is not AgentRole.QUERY_PARSER
        )
        if self.terminal_corrupt:
            if not completed_result or has_result:
                raise ValueError("corrupt terminal marker requires missing completed result")
        elif completed_result != has_result:
            raise ValueError("completed result sessions require a result")
        if self.agent_role is AgentRole.QUERY_PARSER and has_result:
            raise ValueError("query_parser sessions must not have a result")
        if has_result:
            SessionSummary.model_validate(
                self.model_dump(mode="python", exclude={"terminal_corrupt"})
            )
        return self


ReadSession: TypeAlias = SessionSummary | ReadSessionSummary


class ContextSummaryLocation(DomainModel):
    latitude: DomainFloat = Field(ge=-90, le=90)
    longitude: DomainFloat = Field(ge=-180, le=180)
    display_name: NonEmptyString | None = None


class ContextSummaryDateRange(DomainModel):
    start_date: date
    end_date: date


class ContextSummaryParameter(DomainModel):
    code_system: NonEmptyString
    code: NonEmptyString
    canonical_name: NonEmptyString


class ContextSummaryTargetParameter(ContextSummaryParameter):
    source: ParameterIntentSource
    rationale: NonEmptyString


class AnalysisContextSummary(DomainModel):
    target_comid: NonEmptyString
    location: ContextSummaryLocation
    date_range: ContextSummaryDateRange
    mode: InferenceMode
    time_resolution: TimeResolution
    params_used: tuple[ContextSummaryTargetParameter, ...] = Field(
        min_length=1,
        max_length=6,
    )
    supporting_params: tuple[ContextSummaryParameter, ...]


class RunSummary(DomainModel):
    run_id: UUID
    status: RunStatus
    created_at: UtcDatetime
    updated_at: UtcDatetime
    query: NonEmptyString
    configuration: AnalysisConfigurationSnapshot | None = None
    context_summary: AnalysisContextSummary | None
    result_ref: TranscriptContentRef | None
    result_summary: TopologyRunResultSummary | None


class RunHistoryItem(RunSummary):
    issues: tuple[ReadIssue, ...] = ()


class TranscriptScope(DomainModel):
    session_id: UUID | None = None
    agent_role: AgentRole | None = None
    target_comid: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> "TranscriptScope":
        if self.session_id is None:
            if self.agent_role is not None or self.target_comid is not None:
                raise ValueError("run-owned transcript scope must not name an Agent")
            return self
        if self.agent_role is None:
            raise ValueError("session-owned transcript scope requires an Agent role")
        requires_comid = self.agent_role is AgentRole.WATERBODY
        if requires_comid != (self.target_comid is not None):
            raise ValueError("only waterbody scope requires target_comid")
        return self


class TranscriptCorrelation(DomainModel):
    request_id: UUID | None = None
    operation_id: UUID | None = None
    error_id: UUID | None = None


class InlineTranscriptContent(DomainModel):
    kind: Literal["inline"] = "inline"
    body: JsonValue


class ReferencedTranscriptContent(DomainModel):
    kind: Literal["reference"] = "reference"
    preview_head: str
    preview_tail: str
    truncated: Literal[True] = True


class UnparsedTranscriptContent(DomainModel):
    kind: Literal["unparsed"] = "unparsed"


TranscriptContentView: TypeAlias = Annotated[
    InlineTranscriptContent
    | ReferencedTranscriptContent
    | UnparsedTranscriptContent,
    Field(discriminator="kind"),
]


class TranscriptItemView(DomainModel):
    item_id: UUID
    run_id: UUID
    transcript_sequence: int = Field(strict=True, gt=0)
    occurred_at: UtcDatetime
    content_type: NonEmptyString
    content_schema_version: int = Field(strict=True, gt=0)
    scope: TranscriptScope
    correlation: TranscriptCorrelation
    tags: tuple[NonEmptyString, ...] = ()
    rag_eligible: StrictBool = False
    confidence: Annotated[StrictFloat, Field(ge=0, le=1)] | None = None
    provenance: tuple[NonEmptyString, ...] | None = None
    content_bytes: int = Field(strict=True, ge=0)
    content_ref: TranscriptContentRef
    content: TranscriptContentView
    parse_status: ParseStatus
    parse_error: PublicError | None = None


class StoredRunHeader(DomainModel):
    run_id: UUID
    status: RunStatus
    created_at: UtcDatetime
    updated_at: UtcDatetime
    query: NonEmptyString
    request_item_id: UUID
    created_item_id: UUID
    terminal_item_id: UUID | None = None


class StoredRunRead(DomainModel):
    run: StoredRunHeader
    sessions: tuple[ReadSession, ...] = ()
    session_issues: tuple[ReadIssue, ...] = ()
    items: tuple[StoredTranscriptItem, ...] = ()
    contents: tuple[StoredTranscriptContent, ...] = ()


class RegisteredRunRead(DomainModel):
    """One persisted run decoded through the production transcript registry."""

    run: StoredRunHeader
    sessions: tuple[ReadSession, ...] = ()
    items: tuple[ReadTranscriptItem, ...] = ()


class StoredRunHistoryPage(DomainModel):
    runs: tuple[StoredRunRead, ...] = ()


class LiveRunRead(DomainModel):
    run: AnalysisRunSnapshot
    query: NonEmptyString
    sessions: tuple[SessionSummary, ...] = ()
    items: tuple[TranscriptItem, ...] = ()
    persistence: PersistenceInfo


class HistoryCursorPosition(DomainModel):
    created_at: UtcDatetime
    run_id: UUID


class RunHistoryResponse(DomainModel):
    items: tuple[RunHistoryItem, ...] = ()
    next_cursor: str | None = None


class RunDetailResponse(DomainModel):
    run: RunSummary
    sessions: tuple[ReadSession, ...] = ()
    items: tuple[TranscriptItemView, ...] = ()
    referenced_items: tuple[TranscriptItemView, ...] = ()
    source: ReadSource
    persistence: PersistenceInfo | None = None
    issues: tuple[ReadIssue, ...] = ()


class SessionDetailResponse(DomainModel):
    run_id: UUID
    session: ReadSession
    transcript_url: NonEmptyString
    source: ReadSource
    persistence: PersistenceInfo | None = None
    issues: tuple[ReadIssue, ...] = ()


class SessionTranscriptResponse(DomainModel):
    run_id: UUID
    session_id: UUID
    items: tuple[TranscriptItemView, ...] = ()
    referenced_items: tuple[TranscriptItemView, ...] = ()
    source: ReadSource
    persistence: PersistenceInfo | None = None
    issues: tuple[ReadIssue, ...] = ()


class TranscriptContentResponse(DomainModel):
    run_id: UUID
    item_id: UUID
    content_type: NonEmptyString
    content_schema_version: int = Field(strict=True, gt=0)
    content_bytes: int = Field(strict=True, ge=0)
    text: str
    source: ReadSource
    persistence: PersistenceInfo | None = None
    issues: tuple[ReadIssue, ...] = ()

    @model_validator(mode="after")
    def validate_content_bytes(self) -> "TranscriptContentResponse":
        if len(self.text.encode("utf-8")) != self.content_bytes:
            raise ValueError("content_bytes must match text UTF-8 size")
        return self


class RunStatusResponse(DomainModel):
    run_id: UUID
    status: RunStatus
    created_at: UtcDatetime
    updated_at: UtcDatetime
    persistence: PersistenceInfo | None = None


class PersistenceRetryResponse(DomainModel):
    run_id: UUID
    persistence: PersistenceInfo
