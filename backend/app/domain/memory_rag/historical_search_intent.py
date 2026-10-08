from typing import Annotated

from pydantic import Field, field_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.memory_rag.historical_search_strategy import HistoricalSearchStrategy


class HistoricalSearchIntentV1(DomainModel):
    strategy: HistoricalSearchStrategy
    parameter_keys: Annotated[tuple[ParameterKey, ...], Field(min_length=1)]
    window_ids: Annotated[tuple[NonEmptyString, ...], Field(min_length=1)]
    limit: Annotated[int, Field(strict=True, ge=1, le=5)] = 5

    @field_validator("parameter_keys", "window_ids")
    @classmethod
    def validate_unique_inputs(cls, value: tuple[object, ...]) -> tuple[object, ...]:
        return ensure_unique_tuple(value, field_name="historical search inputs")
