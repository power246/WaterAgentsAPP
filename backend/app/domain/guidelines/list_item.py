from uuid import UUID

from app.domain.domain_model import DomainModel
from app.domain.guidelines.canonical import Digest, Revision
from app.domain.guidelines.document_details import GuidelineManagementState
from app.domain.guidelines.identity import GuidelineIdentity
from app.domain.guidelines.source_kind import GuidelineSourceKind


class GuidelineListItem(DomainModel):
    identity: GuidelineIdentity
    revision: Revision
    activation_epoch: Revision
    title: str | None
    summary: str | None
    source_kind: GuidelineSourceKind | None
    draft_digest: Digest | None
    has_unpublished_changes: bool
    active_version_id: UUID | None
    pending_version_id: UUID | None
    effective_version_id: UUID | None
    latest_bundled_version_id: UUID | None
    has_bundled_update: bool
    state: GuidelineManagementState
