from typing import Annotated

from pydantic import Field

from app.domain.common import DomainFloat, DomainModel


class EmbeddingVector(DomainModel):
    values: Annotated[tuple[DomainFloat, ...], Field(min_length=1)]
