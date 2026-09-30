from typing import Annotated, Literal

from pydantic import Field

from app.domain.common import DomainModel, NonEmptyString


class EvidenceDerivation(DomainModel):
    algorithm: NonEmptyString
    algorithm_version: Annotated[int, Field(strict=True, ge=1)]
    policy_id: NonEmptyString
    registry_digest: NonEmptyString
    deterministic: Literal[True] = True
