"""M9 domain contract."""

from typing import Literal
from app.domain.common import DomainModel, NonEmptyString
from .admin_action_kind import AdminActionKind


class ProfileHandoffResult(DomainModel):
    kind: Literal[AdminActionKind.PROFILE_HANDOFF] = AdminActionKind.PROFILE_HANDOFF
    active_generation_id: NonEmptyString
