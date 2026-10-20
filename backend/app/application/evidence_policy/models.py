from typing import Self

from pydantic import StrictBool, field_validator, model_validator

from app.application.query_understanding import EvidenceRule
from app.domain.common import (
    CanonicalDecimal,
    DomainModel,
    NonEmptyString,
    UtcDatetime,
    ensure_unique_tuple,
)
from app.domain.evidence.aggregate_payload import AggregatePayload
from app.domain.evidence.data_quality import DataQualityFlag
from app.domain.evidence.evidence_coverage import EvidenceCoverage
from app.domain.evidence.evidence_derivation import EvidenceDerivation
from app.domain.evidence.evidence_quality_class import EvidenceQualityClass
from app.domain.evidence.source_record_reference import SourceRecordReference
from app.domain.hydrology.location import GeoPoint
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.hydrology.parameter_provider import ParameterProvider
from app.domain.hydrology.value_semantics import ValueSemantics
from app.domain.time.time_range import TimeRange
from app.domain.time.timezone import get_iana_timezone


class UnitConversionResult(DomainModel):
    source_value: CanonicalDecimal
    source_unit_id: NonEmptyString
    source_unit: NonEmptyString
    target_unit_id: NonEmptyString
    target_unit: NonEmptyString
    formula_id: NonEmptyString
    registry_digest: NonEmptyString
    unrounded_value: CanonicalDecimal
    value: CanonicalDecimal


class QualityAssessment(DomainModel):
    quality_class: EvidenceQualityClass
    flags: tuple[DataQualityFlag, ...]
    approval: NonEmptyString | None
    qualifiers: tuple[NonEmptyString, ...]

    @field_validator("flags", "qualifiers")
    @classmethod
    def validate_unique_values(cls, value: tuple[object, ...]) -> tuple[object, ...]:
        return ensure_unique_tuple(value, field_name="quality values")


class PolicyObservation(DomainModel):
    record_ref: SourceRecordReference
    parameter: ParameterKey
    provider: ParameterProvider
    channel: NonEmptyString
    location_comid: NonEmptyString | None = None
    station_id: NonEmptyString
    location_point: GeoPoint | None = None
    medium: NonEmptyString | None
    fraction: NonEmptyString | None
    method: NonEmptyString
    statistic: NonEmptyString
    value_semantics: ValueSemantics
    observed_time: UtcDatetime | None = None
    valid_time_range: TimeRange | None = None
    value: CanonicalDecimal
    unit: NonEmptyString
    quality: QualityAssessment
    series_identity: NonEmptyString

    @model_validator(mode="after")
    def validate_time_identity(self) -> Self:
        if (self.observed_time is None) == (self.valid_time_range is None):
            raise ValueError(
                "exactly one of observed_time or valid_time_range is required"
            )
        return self


class IndirectEvidenceContext(DomainModel):
    rule: EvidenceRule
    input_parameters: tuple[ParameterKey, ...]
    input_windows: tuple[TimeRange, ...]
    analysis_timezone: NonEmptyString
    same_location: StrictBool = False
    reference_complete: StrictBool
    upstream_result_complete: StrictBool = False

    @field_validator("analysis_timezone")
    @classmethod
    def validate_analysis_timezone(cls, value: str) -> str:
        get_iana_timezone(value)
        return value

    @model_validator(mode="after")
    def validate_inputs(self) -> Self:
        if not self.input_parameters:
            raise ValueError("indirect evidence requires input parameters")
        if len(self.input_parameters) != len(self.input_windows):
            raise ValueError("indirect parameters and windows must align")
        ensure_unique_tuple(
            self.input_parameters,
            field_name="indirect input parameters",
        )
        return self


class AggregationOutcome(DomainModel):
    payload: AggregatePayload
    quality_class: EvidenceQualityClass
    quality_flags: tuple[DataQualityFlag, ...]
    coverage: EvidenceCoverage
    derivation: EvidenceDerivation

    @field_validator("quality_flags")
    @classmethod
    def validate_unique_flags(
        cls,
        value: tuple[DataQualityFlag, ...],
    ) -> tuple[DataQualityFlag, ...]:
        return ensure_unique_tuple(value, field_name="quality_flags")
