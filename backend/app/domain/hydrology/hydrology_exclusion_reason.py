from pydantic import field_validator

from app.domain.common import DomainModel, NonEmptyString
from app.domain.hydrology._topology_validation import validate_comid
from app.domain.hydrology.coverage_unit import CoverageUnit
from app.domain.hydrology.exclusion_reason_code import ExclusionReasonCode


class HydrologyExclusionReason(DomainModel):
    code: ExclusionReasonCode
    branch_root_comid: NonEmptyString
    coverage_unit: CoverageUnit
    stop_comid: NonEmptyString
    blocked_upstream_comid: NonEmptyString | None = None

    @field_validator("branch_root_comid", "stop_comid")
    @classmethod
    def validate_comid(cls, value: str) -> str:
        return validate_comid(value)

    @field_validator("blocked_upstream_comid")
    @classmethod
    def validate_optional_comid(cls, value: str | None) -> str | None:
        return validate_comid(value) if value is not None else None
