from typing import Annotated

from pydantic import Field

from app.domain.common import DomainFloat
from app.domain.domain_model import DomainModel
from app.domain.guidelines.version import GuidelineVersionV1


class GuidelineMatch(DomainModel):
    version: GuidelineVersionV1
    similarity: Annotated[DomainFloat, Field(ge=-1, le=1)]
