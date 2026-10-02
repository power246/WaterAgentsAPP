from typing import Annotated

from pydantic import Field

from app.domain.common import DomainModel


class QualitySummary(DomainModel):
    valid: Annotated[int, Field(strict=True, ge=0)] = 0
    suspicious: Annotated[int, Field(strict=True, ge=0)] = 0
    invalid: Annotated[int, Field(strict=True, ge=0)] = 0
