"""M9 domain contract."""

from typing import Literal
from app.domain.common import DomainModel, NonEmptyString
from .admin_action_kind import AdminActionKind


class GenerationDeletionResult(DomainModel):
    kind: Literal[AdminActionKind.GENERATION_DELETION] = AdminActionKind.GENERATION_DELETION
    deleted_generation_id: NonEmptyString
