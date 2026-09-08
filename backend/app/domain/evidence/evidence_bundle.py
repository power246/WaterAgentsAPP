from typing import Self

from pydantic import field_validator, model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.evidence.evidence_fragment import EvidenceFragment
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.time.time_range import TimeRange
from app.domain.time.time_resolution import DEFAULT_TIME_RESOLUTION, TimeResolution
from app.domain.time.time_resolution_source import TimeResolutionSource
from app.domain.time.timezone import get_iana_timezone


class EvidenceBundle(DomainModel):
    bundle_id: NonEmptyString
    agent_comid: NonEmptyString
    time_range: TimeRange
    analysis_timezone: NonEmptyString
    time_resolution: TimeResolution = DEFAULT_TIME_RESOLUTION
    time_resolution_source: TimeResolutionSource
    requested_parameters: tuple[ParameterKey, ...]
    fragments: tuple[EvidenceFragment, ...] = ()

    @field_validator("analysis_timezone")
    @classmethod
    def validate_analysis_timezone(cls, value: str) -> str:
        get_iana_timezone(value)
        return value

    @field_validator("requested_parameters")
    @classmethod
    def validate_requested_parameters(
        cls,
        value: tuple[ParameterKey, ...],
    ) -> tuple[ParameterKey, ...]:
        if not value:
            raise ValueError("requested_parameters must not be empty")
        return ensure_unique_tuple(value, field_name="requested_parameters")

    @model_validator(mode="after")
    def validate_fragment_ids(self) -> Self:
        evidence_ids = tuple(fragment.evidence_id for fragment in self.fragments)
        ensure_unique_tuple(evidence_ids, field_name="fragment evidence IDs")
        return self
