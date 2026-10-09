"""M9 domain contract."""

from typing import Literal, Self
from pydantic import model_validator
from app.domain.common import DomainModel, UtcDatetime
from .primitives import Revision
from .mutable_defaults import MutableDefaults
from .unit_policy_projection import UnitPolicyProjection


class SettingsSnapshot(DomainModel):
    schema_version: Literal[1] = 1
    revision: Revision
    defaults: MutableDefaults
    unit_policy: UnitPolicyProjection
    updated_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def validate_revision_timestamp(self) -> Self:
        if (self.revision == 0) != (self.updated_at is None):
            raise ValueError("revision zero is the only unsaved settings snapshot")
        return self
