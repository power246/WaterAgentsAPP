from uuid import UUID

from pydantic import Field, StrictInt, StrictStr

from app.application.analysis_sessions.models import (
    AnalysisConfigurationSnapshot,
    AnalysisSessionSnapshot,
    PublicError,
    StableCode,
)
from app.domain.common import NonEmptyString
from app.domain.domain_model import DomainModel
from app.domain.time.time_resolution import DEFAULT_TIME_RESOLUTION, TimeResolution
from app.domain.time.time_resolution_source import TimeResolutionSource


class CreateAnalysisSessionRequest(DomainModel):
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


class AnalysisSessionResponse(AnalysisSessionSnapshot):
    pass


class HttpPublicError(PublicError):
    request_id: UUID
    session_id: UUID | None = None


class ValidationViolation(DomainModel):
    field: tuple[StrictStr | StrictInt, ...]
    type: StableCode
    message: NonEmptyString


class AnalysisSessionErrorResponse(DomainModel):
    error: HttpPublicError
    session: AnalysisSessionResponse | None = None
    validation: tuple[ValidationViolation, ...] = ()
