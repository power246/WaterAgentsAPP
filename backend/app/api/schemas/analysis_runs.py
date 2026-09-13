from uuid import UUID

from pydantic import Field, StrictInt, StrictStr

from app.application.analysis_runs.models import (
    AnalysisConfigurationSnapshot,
    AnalysisRunSnapshot,
    PublicError,
    StableCode,
)
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
from app.domain.time.time_resolution import DEFAULT_TIME_RESOLUTION, TimeResolution
from app.domain.time.time_resolution_source import TimeResolutionSource


class CreateAnalysisRunRequest(DomainModel):
    query: NonEmptyString = Field(repr=False)
    time_resolution: TimeResolution = DEFAULT_TIME_RESOLUTION

    def to_configuration_snapshot(
        self, analysis_timezone: str
    ) -> AnalysisConfigurationSnapshot:
        source = (
            TimeResolutionSource.FRONTEND
            if "time_resolution" in self.model_fields_set
            else TimeResolutionSource.DEFAULT
        )
        return AnalysisConfigurationSnapshot(
            analysis_timezone=analysis_timezone,
            time_resolution=self.time_resolution,
            time_resolution_source=source,
        )


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
