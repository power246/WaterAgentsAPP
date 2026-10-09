"""M9 domain contract."""

from typing import Annotated, Self
from pydantic import Field, model_validator
from app.domain.common import DomainModel
from .primitives import Digest, Version
from .unit_policy_entry import UnitPolicyEntry


class UnitPolicyProjection(DomainModel):
    version: Version
    registry_digest: Digest
    entries: Annotated[tuple[UnitPolicyEntry, ...], Field(max_length=154)]

    @model_validator(mode="after")
    def validate_entries(self) -> Self:
        identities = tuple(entry.parameter for entry in self.entries)
        if len(identities) != len(set(identities)):
            raise ValueError("unit policy parameters must be unique")
        return self
