from typing import Self

from pydantic import field_validator, model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.hydrology._topology_validation import validate_comid
from app.domain.hydrology.branch_outcome_reason import BranchOutcomeReason
from app.domain.hydrology.branch_outcome_status import BranchOutcomeStatus
from app.domain.hydrology.coverage_unit import CoverageUnit


class BranchOutcome(DomainModel):
    branch_root_comid: NonEmptyString
    coverage_unit: CoverageUnit
    outcome: BranchOutcomeStatus
    stop_comid: NonEmptyString
    path_to_target: tuple[NonEmptyString, ...]
    reason: BranchOutcomeReason
    blocked_upstream_comid: NonEmptyString | None = None

    @field_validator("branch_root_comid", "stop_comid")
    @classmethod
    def validate_comid(cls, value: str) -> str:
        return validate_comid(value)

    @field_validator("blocked_upstream_comid")
    @classmethod
    def validate_optional_comid(cls, value: str | None) -> str | None:
        return validate_comid(value) if value is not None else None

    @field_validator("path_to_target")
    @classmethod
    def validate_path(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("path_to_target must not be empty")
        for comid in value:
            validate_comid(comid)
        ensure_unique_tuple(value, field_name="path_to_target")
        return value

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.path_to_target[0] != self.stop_comid:
            raise ValueError("outcome path must start at stop_comid")
        if self.branch_root_comid not in self.path_to_target:
            raise ValueError("outcome path must contain branch_root_comid")
        qualified_reasons = {
            BranchOutcomeReason.TARGET_STATION_QUALIFIED,
            BranchOutcomeReason.STATION_QUALIFIED,
        }
        limit_reasons = {
            BranchOutcomeReason.MAX_DISTANCE_REACHED,
            BranchOutcomeReason.MAX_HOPS_REACHED,
            BranchOutcomeReason.MAX_NODES_REACHED,
            BranchOutcomeReason.MULTIPLE_LIMITS_REACHED,
        }
        if self.outcome is BranchOutcomeStatus.QUALIFIED:
            if self.reason not in qualified_reasons:
                raise ValueError("qualified outcome requires a station reason")
        elif self.outcome is BranchOutcomeStatus.NO_QUALIFYING_SOURCE:
            if self.reason is not BranchOutcomeReason.NATURAL_HEADWATER:
                raise ValueError("natural exhaustion requires natural_headwater")
        elif self.reason not in limit_reasons:
            raise ValueError("limit outcome requires a limit reason")
        if (
            self.blocked_upstream_comid is not None
            and self.outcome is not BranchOutcomeStatus.SEARCH_LIMIT_REACHED
        ):
            raise ValueError("only a limit outcome may identify a blocked upstream")
        if self.blocked_upstream_comid == self.stop_comid:
            raise ValueError("blocked upstream must differ from stop_comid")
        return self
