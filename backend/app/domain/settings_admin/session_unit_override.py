"""M9 domain contract."""

from typing import Literal
from app.domain.common import DomainModel, NonEmptyString
from app.domain.hydrology.parameter_key import ParameterKey
from .primitives import Digest


class SessionUnitOverride(DomainModel):
    parameter: ParameterKey
    target_unit_id: NonEmptyString
    registry_digest: Digest
    source: Literal["session_override"] = "session_override"
