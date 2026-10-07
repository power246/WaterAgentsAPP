from typing import Annotated, Self

from pydantic import Field, StrictBool, StrictInt, model_validator

from app.domain.common import DomainModel, NonEmptyString
from app.domain.memory_rag.rag_profile_handoff_phase import (
    RAGProfileHandoffPhase,
)


class RAGProfileHandoffState(DomainModel):
    desired_profile_digest: NonEmptyString
    phase: RAGProfileHandoffPhase
    active_generation_id: NonEmptyString | None = None
    building_generation_id: NonEmptyString | None = None
    retired_generation_ids: tuple[NonEmptyString, ...] = ()
    blocking_reader_count: Annotated[StrictInt, Field(ge=0)] = 0
    old_credential_cleanup_ready: StrictBool = False

    @model_validator(mode="after")
    def validate_phase(self) -> Self:
        ready = self.phase is RAGProfileHandoffPhase.CREDENTIAL_CLEANUP_READY
        if self.old_credential_cleanup_ready is not ready:
            raise ValueError("credential cleanup readiness must match handoff phase")
        if self.phase is RAGProfileHandoffPhase.READER_DRAIN:
            if self.blocking_reader_count < 1:
                raise ValueError("reader drain requires at least one blocking reader")
        elif self.blocking_reader_count:
            raise ValueError("only reader drain may report blocking readers")
        return self
