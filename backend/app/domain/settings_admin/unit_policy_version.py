"""M9 domain contract."""

from app.domain.common import UtcDatetime
from .unit_policy_projection import UnitPolicyProjection


class UnitPolicyVersion(UnitPolicyProjection):
    created_at: UtcDatetime
