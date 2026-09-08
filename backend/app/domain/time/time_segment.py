from typing import Self

from pydantic import model_validator

from app.domain.inference.mode import InferenceMode
from app.domain.time.time_range import TimeRange


class TimeSegment(TimeRange):
    mode: InferenceMode

    @model_validator(mode="after")
    def reject_mixed_mode(self) -> Self:
        if self.mode is InferenceMode.MIXED:
            raise ValueError("time segment mode must not be mixed")
        return self
