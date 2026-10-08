"""Closed, bounded recovery payload for a committed Admin operation."""

from typing import Annotated, Self
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, StringConstraints, model_validator

from app.domain.common import DomainModel
from .admin_action_kind import AdminActionKind


SafeToken = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]
DigestHex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class AdminCommand(DomainModel):
    kind: AdminActionKind
    resource_identity: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    facts_digest: DigestHex
    requested_run_ids: Annotated[tuple[UUID, ...], Field(max_length=500)] = ()
    all_history: StrictBool = False
    expected_delete_run_ids: Annotated[tuple[UUID, ...], Field(max_length=500)] = ()
    expected_dependency_ids: Annotated[tuple[UUID, ...], Field(max_length=500)] = ()
    cascade_consumers: StrictBool = False
    generation_id: Annotated[str, StringConstraints(min_length=1, max_length=256)] | None = None
    safe_error_codes: Annotated[tuple[SafeToken, ...], Field(max_length=100)] = ()
    limit: Annotated[StrictInt, Field(ge=1, le=100)] | None = None
    desired_profile_id: Annotated[str, StringConstraints(min_length=1, max_length=256)] | None = None

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        for values in (
            self.requested_run_ids, self.expected_delete_run_ids,
            self.expected_dependency_ids, self.safe_error_codes,
        ):
            if len(values) != len(set(values)):
                raise ValueError("Admin command identities must be unique")
        if self.kind is AdminActionKind.HISTORY_CLEANUP:
            if not self.requested_run_ids or not self.expected_delete_run_ids:
                raise ValueError("history command requires selected runs")
            if self.generation_id or self.safe_error_codes or self.limit or self.desired_profile_id:
                raise ValueError("history command has unrelated fields")
        elif self.kind is AdminActionKind.PROFILE_HANDOFF:
            if not self.desired_profile_id or self.generation_id or self.requested_run_ids or self.safe_error_codes or self.all_history or self.limit:
                raise ValueError("profile command has invalid fields")
        elif self.kind is AdminActionKind.RETRY_FAILED_JOBS:
            if not self.generation_id or not self.safe_error_codes or self.limit is None:
                raise ValueError("retry command requires generation, codes, and limit")
            if self.requested_run_ids or self.desired_profile_id or self.all_history:
                raise ValueError("retry command has unrelated fields")
        elif self.kind is AdminActionKind.GENERATION_DELETION:
            if not self.generation_id or self.requested_run_ids or self.safe_error_codes or self.limit or self.desired_profile_id or self.all_history:
                raise ValueError("generation deletion command has invalid fields")
        if self.kind is not AdminActionKind.HISTORY_CLEANUP and (
            self.expected_delete_run_ids or self.expected_dependency_ids
            or self.cascade_consumers
        ):
            raise ValueError("non-history command has cleanup fields")
        return self
