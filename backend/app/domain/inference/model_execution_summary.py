from typing import Annotated

from pydantic import Field, StrictInt

from app.domain.common import DomainModel, NonEmptyString
from app.domain.inference.model_usage_summary import ModelUsageSummary
from app.domain.model_selection import ModelSelection


class ModelExecutionSummary(DomainModel):
    model: ModelSelection
    planning_calls: Annotated[StrictInt, Field(ge=0)]
    tool_calls: Annotated[StrictInt, Field(ge=0)]
    reasoning_batches: Annotated[StrictInt, Field(ge=0)]
    repair_calls: Annotated[StrictInt, Field(ge=0)]
    synthesis_calls: Annotated[StrictInt, Field(ge=0)]
    skipped_cells: Annotated[StrictInt, Field(ge=0)]
    numeric_estimation_calls: Annotated[StrictInt, Field(ge=0)] = 0
    numeric_failure_codes: tuple[NonEmptyString, ...] = ()
    usage: ModelUsageSummary | None = None
