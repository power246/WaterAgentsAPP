from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.domain.common import NonEmptyString
from app.domain.domain_model import DomainModel


class GuidelineUsageTarget(DomainModel):
    kind: Literal["numeric_task", "cell", "window", "analysis_step", "overall"]
    task_id: NonEmptyString | None = None
    cell_id: NonEmptyString | None = None
    window_id: NonEmptyString | None = None
    step_index: Annotated[int, Field(strict=True, ge=0)] | None = None
    key: Literal["overall"] | None = None

    @model_validator(mode="after")
    def exact_target_fields(self) -> Self:
        required = {
            "numeric_task": {"task_id", "window_id"},
            "cell": {"cell_id", "window_id"},
            "window": {"window_id"},
            "analysis_step": {"step_index"},
            "overall": {"key"},
        }[self.kind]
        provided = {
            name for name in ("task_id", "cell_id", "window_id", "step_index", "key")
            if getattr(self, name) is not None
        }
        if provided != required:
            raise ValueError("usage target fields do not match its kind")
        return self
