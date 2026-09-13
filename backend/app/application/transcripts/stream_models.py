from dataclasses import dataclass
from typing import Literal, TypeAlias
from uuid import UUID

from pydantic import Field, StrictBool

from app.application.analysis_runs.models import (
    AgentSessionStatus,
    AnalysisRunSnapshot,
    RunStatus,
    SessionSummary,
)
from app.application.analysis_runs.persistence import PersistenceInfo
from app.application.transcripts.models import GenerationStatus, TranscriptItem
from app.application.transcripts.read_models import (
    TranscriptCorrelation,
    TranscriptItemView,
    TranscriptScope,
)
from app.application.transcripts.streaming import StreamingCredentialFilter
from app.domain.common import NonEmptyString, UtcDatetime
from app.domain.domain_model import DomainModel


class RunWaterbodySummary(DomainModel):
    target_comid: NonEmptyString
    participating: StrictBool
    session_id: UUID | None = None
    session_status: AgentSessionStatus | None = None


class RunSnapshotEvent(DomainModel):
    schema_version: Literal[1] = 1
    run_id: UUID
    status: RunStatus
    created_at: UtcDatetime
    updated_at: UtcDatetime
    sessions: tuple[SessionSummary, ...] = ()
    waterbodies: tuple[RunWaterbodySummary, ...] = ()
    persistence: PersistenceInfo


class TranscriptItemHeader(DomainModel):
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


class TranscriptItemPayload(DomainModel):
    item: TranscriptItemView


class MessageStartedPayload(DomainModel):
    item: TranscriptItemHeader


class MessageDeltaPayload(DomainModel):
    item_id: UUID
    text: str


class MessageFinalizedPayload(DomainModel):
    item: TranscriptItemView
    generation_status: GenerationStatus


SessionProcessPayload: TypeAlias = (
    TranscriptItemPayload
    | MessageStartedPayload
    | MessageDeltaPayload
    | MessageFinalizedPayload
)


class SessionProcessEvent(DomainModel):
    schema_version: Literal[1] = 1
    run_id: UUID
    session_id: UUID
    sequence: int = Field(strict=True, gt=0)
    occurred_at: UtcDatetime
    payload: SessionProcessPayload


class StreamEndEvent(DomainModel):
    schema_version: Literal[1] = 1
    run_id: UUID
    session_id: UUID | None = None
    stream: Literal["run", "session"]


@dataclass(frozen=True, slots=True)
class TranscriptItemProcessRecord:
    sequence: int
    occurred_at: UtcDatetime
    item: TranscriptItem


@dataclass(frozen=True, slots=True)
class MessageStartedProcessRecord:
    sequence: int
    occurred_at: UtcDatetime
    header: TranscriptItemHeader


@dataclass(frozen=True, slots=True)
class MessageDeltaProcessRecord:
    sequence: int
    occurred_at: UtcDatetime
    item_id: UUID
    text: str


@dataclass(frozen=True, slots=True)
class MessageFinalizedProcessRecord:
    sequence: int
    occurred_at: UtcDatetime
    item: TranscriptItem
    generation_status: GenerationStatus


SessionProcessRecord: TypeAlias = (
    TranscriptItemProcessRecord
    | MessageStartedProcessRecord
    | MessageDeltaProcessRecord
    | MessageFinalizedProcessRecord
)


@dataclass(slots=True)
class StreamingMessage:
    item_id: UUID
    session_id: UUID
    content_type: str
    content_schema_version: int
    filter: StreamingCredentialFilter
    private_values: tuple[str, ...] = ()
    safe_text: str = ""


def snapshot_from_state(
    snapshot: AnalysisRunSnapshot,
    sessions: tuple[SessionSummary, ...],
    waterbodies: tuple[RunWaterbodySummary, ...],
    persistence: PersistenceInfo,
) -> RunSnapshotEvent:
    return RunSnapshotEvent(
        run_id=snapshot.run_id,
        status=snapshot.status,
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
        sessions=sessions,
        waterbodies=waterbodies,
        persistence=persistence,
    )
