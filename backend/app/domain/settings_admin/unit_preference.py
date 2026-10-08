"""Resolved target preference for one sealed parameter."""

from typing import Literal

from app.domain.common import DomainModel, NonEmptyString
from app.domain.hydrology.parameter_key import ParameterKey
from .primitives import Digest


class UnitPreference(DomainModel):
    parameter: ParameterKey
    target_unit_id: NonEmptyString
    registry_digest: Digest
    source: Literal["session_override", "global_policy"]
