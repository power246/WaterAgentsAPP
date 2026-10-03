from typing import Annotated, Self

from pydantic import Field, StrictInt, model_validator

from app.domain.common import DomainModel


class ModelUsageSummary(DomainModel):
    input_tokens: Annotated[StrictInt, Field(ge=0)] | None = None
    output_tokens: Annotated[StrictInt, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def validate_usage(self) -> Self:
        if self.input_tokens is None and self.output_tokens is None:
            raise ValueError("usage requires at least one token count")
        return self
