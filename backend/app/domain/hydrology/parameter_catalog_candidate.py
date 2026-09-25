from typing import Annotated

from pydantic import Field

from app.domain.common import DomainFloat, DomainModel, NonEmptyString
from app.domain.hydrology.parameter_key import ParameterKey


class ParameterCatalogCandidate(DomainModel):
    key: ParameterKey
    canonical_name: NonEmptyString
    score: Annotated[DomainFloat, Field(ge=0.0, le=1.0)]
