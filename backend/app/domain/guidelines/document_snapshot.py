from typing import Self
from uuid import UUID

from pydantic import model_validator

from app.domain.domain_model import DomainModel
from app.domain.guidelines.canonical import Revision
from app.domain.guidelines.draft import GuidelineDraftV1
from app.domain.guidelines.identity import GuidelineIdentity
from app.domain.guidelines.origin import GuidelineOrigin


class GuidelineDocumentSnapshot(DomainModel):
    identity: GuidelineIdentity
    revision: Revision
    activation_epoch: Revision
    draft: GuidelineDraftV1 | None = None
    active_version_id: UUID | None = None
    pending_version_id: UUID | None = None
    disabled: bool = False

    @model_validator(mode="after")
    def content_authority(self) -> Self:
        if self.identity.origin is GuidelineOrigin.BUNDLED and self.draft is not None:
            raise ValueError("bundled documents do not own editable drafts")
        return self
