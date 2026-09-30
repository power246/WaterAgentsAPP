from typing import Annotated

from pydantic import Field

from app.domain.common import DomainModel


class CollectionCounts(DomainModel):
    requests: Annotated[int, Field(strict=True, ge=0)] = 0
    pages: Annotated[int, Field(strict=True, ge=0)] = 0
    records: Annotated[int, Field(strict=True, ge=0)] = 0
    response_bytes: Annotated[int, Field(strict=True, ge=0)] = 0
