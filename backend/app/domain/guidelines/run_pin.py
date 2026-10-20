from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from app.domain.domain_model import DomainModel
from app.domain.guidelines.catalog_snapshot import GuidelineCatalogSnapshot

SafeReason = Annotated[str, Field(pattern=r"^guideline_[a-z0-9_]{1,80}$")]


class GuidelineRunPin(DomainModel):
    schema_version: Literal[1] = 1
    run_id: UUID
    policy_version: Literal[1] = 1
    status: Literal["ready", "unavailable"]
    catalog: GuidelineCatalogSnapshot | None = None
    safe_error_code: SafeReason | None = None

    @model_validator(mode="after")
    def consistent_readiness(self) -> Self:
        if self.status == "ready" and (self.catalog is None or self.catalog.profile is None):
            raise ValueError("a ready guideline pin requires a generation/profile")
        if (self.status == "unavailable") != (self.safe_error_code is not None):
            raise ValueError("an unavailable guideline pin requires a safe reason")
        return self
