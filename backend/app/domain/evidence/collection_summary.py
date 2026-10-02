from typing import Annotated

from pydantic import Field

from app.domain.common import DomainModel


class CollectionSummary(DomainModel):
    available: Annotated[int, Field(strict=True, ge=0)] = 0
    empty: Annotated[int, Field(strict=True, ge=0)] = 0
    unavailable: Annotated[int, Field(strict=True, ge=0)] = 0
    invalid: Annotated[int, Field(strict=True, ge=0)] = 0
