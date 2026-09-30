from decimal import Decimal
from typing import Annotated, Self

from pydantic import Field, model_validator

from app.domain.common import CanonicalDecimal, DomainModel
from app.domain.evidence.coverage_status import CoverageStatus


class EvidenceCoverage(DomainModel):
    status: CoverageStatus
    observed_count: Annotated[int, Field(strict=True, ge=0)]
    expected_count: Annotated[int, Field(strict=True, ge=0)] | None = None
    ratio: CanonicalDecimal | None = None
    covered_duration_seconds: Annotated[int, Field(strict=True, ge=0)]
    window_duration_seconds: Annotated[int, Field(strict=True, gt=0)]
    largest_gap_seconds: Annotated[int, Field(strict=True, ge=0)] | None = None

    @model_validator(mode="after")
    def validate_counts_and_ratio(self) -> Self:
        if self.ratio is not None:
            ratio = Decimal(self.ratio)
            if ratio < 0 or ratio > 1:
                raise ValueError("ratio must be between 0 and 1")
            if self.expected_count is None and self.window_duration_seconds == 0:
                raise ValueError("ratio requires count or duration context")
        if self.covered_duration_seconds > self.window_duration_seconds:
            raise ValueError("covered duration must not exceed window duration")
        if self.status is CoverageStatus.COMPLETE and self.observed_count == 0:
            raise ValueError("complete coverage requires observations")
        return self
