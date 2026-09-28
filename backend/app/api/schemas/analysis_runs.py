from uuid import UUID

from pydantic import StrictInt, StrictStr

from app.application.analysis_runs.models import (
    AnalysisRunSnapshot,
    PublicError,
    StableCode,
)
from app.application.transcripts.models import AnalysisRequestV1
from app.application.transcripts.read_models import (
    PersistenceRetryResponse,
    RunDetailResponse,
    RunHistoryResponse,
    RunStatusResponse,
    SessionDetailResponse,
    SessionTranscriptResponse,
    TranscriptContentResponse,
)
from app.domain.common import NonEmptyString
from app.domain.domain_model import DomainModel


class CreateAnalysisRunRequest(AnalysisRequestV1):
    pass


class AnalysisRunResponse(AnalysisRunSnapshot):
    pass


class HttpPublicError(PublicError):
    request_id: UUID
    run_id: UUID | None = None
    session_id: UUID | None = None


class ValidationViolation(DomainModel):
    field: tuple[StrictStr | StrictInt, ...]
    type: StableCode
    message: NonEmptyString


class AnalysisRunErrorResponse(DomainModel):
    error: HttpPublicError
    run: RunStatusResponse | None = None
    validation: tuple[ValidationViolation, ...] = ()
    history_url: StrictStr | None = None


__all__ = [
    "AnalysisRunErrorResponse",
    "AnalysisRunResponse",
    "CreateAnalysisRunRequest",
    "HttpPublicError",
    "PersistenceRetryResponse",
    "RunDetailResponse",
    "RunHistoryResponse",
    "RunStatusResponse",
    "SessionDetailResponse",
    "SessionTranscriptResponse",
    "TranscriptContentResponse",
    "ValidationViolation",
]
