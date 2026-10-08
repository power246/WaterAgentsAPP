from typing import Annotated

from pydantic import Field, StrictInt

from app.domain.common import DomainModel


class RAGJobCounts(DomainModel):
    pending: Annotated[StrictInt, Field(ge=0)] = 0
    leased: Annotated[StrictInt, Field(ge=0)] = 0
    retry_wait: Annotated[StrictInt, Field(ge=0)] = 0
    completed: Annotated[StrictInt, Field(ge=0)] = 0
    excluded: Annotated[StrictInt, Field(ge=0)] = 0
    failed: Annotated[StrictInt, Field(ge=0)] = 0

    @property
    def total(self) -> int:
        return sum(
            (
                self.pending,
                self.leased,
                self.retry_wait,
                self.completed,
                self.excluded,
                self.failed,
            )
        )
