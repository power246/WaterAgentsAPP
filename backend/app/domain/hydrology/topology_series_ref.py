from typing import Annotated, Self

from pydantic import Field, model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.hydrology.parameter_key import ParameterKey


SegmentIndex = Annotated[int, Field(strict=True, ge=0)]


class TopologySeriesRef(DomainModel):
    series_identity: NonEmptyString
    parameter: ParameterKey
    channel: NonEmptyString
    covered_segment_indexes: tuple[SegmentIndex, ...]
    covered_window_ids: tuple[NonEmptyString, ...] | None = None

    @model_validator(mode="after")
    def validate_coverage(self) -> Self:
        if not self.covered_segment_indexes and not self.covered_window_ids:
            raise ValueError("series coverage must include a segment or window")
        ensure_unique_tuple(
            self.covered_segment_indexes,
            field_name="covered_segment_indexes",
        )
        if self.covered_segment_indexes != tuple(
            sorted(self.covered_segment_indexes)
        ):
            raise ValueError("covered_segment_indexes must be sorted")
        if self.covered_window_ids is not None:
            ensure_unique_tuple(
                self.covered_window_ids,
                field_name="covered_window_ids",
            )
            if self.covered_window_ids != tuple(sorted(self.covered_window_ids)):
                raise ValueError("covered_window_ids must be sorted")
        return self
