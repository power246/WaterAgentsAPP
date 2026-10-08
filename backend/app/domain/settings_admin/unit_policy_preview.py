"""M9 domain contract."""

from typing import Self
from pydantic import model_validator
from app.domain.common import DomainModel
from .primitives import Version
from .unit_policy_change import UnitPolicyChange
from .unit_policy_projection import UnitPolicyProjection


class UnitPolicyPreview(DomainModel):
    base_version: Version
    proposed: UnitPolicyProjection
    changes: tuple[UnitPolicyChange, ...]

    @model_validator(mode="after")
    def validate_next_version(self) -> Self:
        if self.proposed.version != self.base_version + 1:
            raise ValueError("preview version must follow base version")
        return self
