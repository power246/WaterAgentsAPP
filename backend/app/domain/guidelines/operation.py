from typing import Annotated, Self
from uuid import UUID

from pydantic import Field, model_validator

from app.domain.common import UtcDatetime
from app.domain.domain_model import DomainModel
from app.domain.guidelines.canonical import Revision
from app.domain.guidelines.mutation_receipt import GuidelineMutationReceipt
from app.domain.guidelines.operation_state import GuidelineOperationState
from app.domain.guidelines.policy import DEFAULT_GUIDELINE_POLICY


class GuidelineOperation(DomainModel):
    receipt: GuidelineMutationReceipt
    state: GuidelineOperationState
    activation_epoch: Revision | None = None
    generation_id: UUID | None = None
    attempts: Annotated[int, Field(strict=True, ge=0, le=DEFAULT_GUIDELINE_POLICY.max_attempts)] = 0
    lease_owner: Annotated[str, Field(min_length=1, max_length=160)] | None = None
    lease_until: UtcDatetime | None = None
    safe_error_code: Annotated[str, Field(pattern=r"^guideline_[a-z_]{1,80}$")] | None = None

    @model_validator(mode="after")
    def valid_claim(self) -> Self:
        running = self.state is GuidelineOperationState.RUNNING
        if running != (self.lease_owner is not None and self.lease_until is not None):
            raise ValueError("running operation requires a complete claim lease")
        if not running and (self.lease_owner is not None or self.lease_until is not None):
            raise ValueError("inactive operation cannot retain a claim lease")
        if running and self.attempts == 0:
            raise ValueError("claimed operation requires an attempt")
        if (self.state is GuidelineOperationState.FAILED) != (self.safe_error_code is not None):
            raise ValueError("only failed operations require a safe error code")
        return self
