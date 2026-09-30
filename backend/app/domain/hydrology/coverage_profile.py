from decimal import Decimal
from typing import Self

from pydantic import Field, StrictInt, model_validator

from app.domain.common import CanonicalDecimal, DomainModel, NonEmptyString
from app.domain.hydrology.coverage_policy_kind import CoveragePolicyKind


class CoverageProfile(DomainModel):
    profile_id: NonEmptyString
    extends_profile_id: NonEmptyString | None = None
    policy_kind: CoveragePolicyKind
    minimum_usable_count: StrictInt = Field(ge=0)
    complete_ratio: CanonicalDecimal | None = None
    partial_ratio: CanonicalDecimal | None = None
    max_gap_cadence_multiplier: CanonicalDecimal | None = None

    @model_validator(mode="after")
    def validate_profile(self) -> Self:
        ratios = (self.complete_ratio, self.partial_ratio)
        if any(
            value is not None and not Decimal("0") <= Decimal(value) <= Decimal("1")
            for value in ratios
        ):
            raise ValueError("coverage ratios must be between zero and one")
        if (
            self.complete_ratio is not None
            and self.partial_ratio is not None
            and Decimal(self.complete_ratio) < Decimal(self.partial_ratio)
        ):
            raise ValueError("complete ratio must not be below partial ratio")
        if (
            self.max_gap_cadence_multiplier is not None
            and Decimal(self.max_gap_cadence_multiplier) <= 0
        ):
            raise ValueError("gap multiplier must be positive")
        return self
