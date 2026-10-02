from typing import Annotated, Self

from pydantic import Field, StrictInt, model_validator

from app.domain.common import (
    DomainFloat,
    DomainModel,
    NonEmptyString,
    ensure_unique_tuple,
)
from app.domain.inference.assessment_availability import AssessmentAvailability
from app.domain.inference.risk_level import RiskLevel
from app.domain.time.time_range import TimeRange


class OverallWaterbodyAssessment(DomainModel):
    assessment_id: NonEmptyString
    time_range: TimeRange
    risk_level: RiskLevel
    risk_rationale: NonEmptyString
    confidence: Annotated[DomainFloat, Field(ge=0, le=1)]
    confidence_rationale: NonEmptyString
    availability: AssessmentAvailability
    assessed_cell_count: Annotated[StrictInt, Field(ge=0)]
    total_cell_count: Annotated[StrictInt, Field(gt=0)]
    contributing_window_refs: tuple[NonEmptyString, ...]
    contributing_cell_refs: tuple[NonEmptyString, ...]
    missing_cell_refs: tuple[NonEmptyString, ...]
    blocker_refs: tuple[NonEmptyString, ...]
    evidence_refs: tuple[NonEmptyString, ...]

    @model_validator(mode="after")
    def validate_assessment(self) -> Self:
        for field_name, refs in (
            ("contributing_window_refs", self.contributing_window_refs),
            ("contributing_cell_refs", self.contributing_cell_refs),
            ("missing_cell_refs", self.missing_cell_refs),
            ("blocker_refs", self.blocker_refs),
            ("evidence_refs", self.evidence_refs),
        ):
            ensure_unique_tuple(refs, field_name=field_name)
        if set(self.contributing_cell_refs) & set(self.missing_cell_refs):
            raise ValueError("contributing and missing cell refs must be disjoint")
        if self.assessed_cell_count > self.total_cell_count:
            raise ValueError("assessed_cell_count must not exceed total_cell_count")
        if len(self.missing_cell_refs) != (
            self.total_cell_count - self.assessed_cell_count
        ):
            raise ValueError("counts must agree with missing_cell_refs")
        if self.assessed_cell_count and (
            not self.contributing_window_refs
            or not self.contributing_cell_refs
            or not self.evidence_refs
        ):
            raise ValueError("assessed cells require window, cell, and evidence refs")

        if self.availability is AssessmentAvailability.COMPLETE:
            if (
                self.assessed_cell_count != self.total_cell_count
                or self.missing_cell_refs
                or self.blocker_refs
            ):
                raise ValueError("complete availability requires complete coverage")
        elif self.availability is AssessmentAvailability.PARTIAL:
            if self.assessed_cell_count == 0 or (
                self.assessed_cell_count == self.total_cell_count
                and not self.blocker_refs
            ):
                raise ValueError(
                    "partial availability requires assessed and missing or blocked facts"
                )
        else:
            if (
                self.assessed_cell_count != 0
                or len(self.missing_cell_refs) != self.total_cell_count
                or self.contributing_window_refs
                or self.contributing_cell_refs
            ):
                raise ValueError("indeterminate availability requires all cells missing")
            if self.risk_level is not RiskLevel.INDETERMINATE:
                raise ValueError("indeterminate availability requires indeterminate risk")
            if self.confidence != 0:
                raise ValueError("indeterminate availability requires confidence 0")

        if self.risk_level is RiskLevel.NONE and (
            not self.contributing_cell_refs or not self.evidence_refs
        ):
            raise ValueError("none risk requires supporting result and evidence refs")
        return self
