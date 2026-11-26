from typing import Literal
from uuid import UUID

from app.domain.guidelines.canonical import Digest, Revision
from app.domain.guidelines.document_snapshot import GuidelineDocumentSnapshot
from app.domain.guidelines.operation import GuidelineOperation
from app.domain.guidelines.version import GuidelineVersionV1

GuidelineManagementState = Literal["draft", "preparing", "available", "preparation_failed", "disabled", "unavailable"]


class GuidelineDocumentDetails(GuidelineDocumentSnapshot):
    draft_digest: Digest | None = None
    has_unpublished_changes: bool = False
    active_version: GuidelineVersionV1 | None = None
    pending_version: GuidelineVersionV1 | None = None
    effective_version_id: UUID | None = None
    latest_version_number: Revision = 0
    latest_bundled_version: GuidelineVersionV1 | None = None
    preparation: GuidelineOperation | None = None
    state: GuidelineManagementState
    has_bundled_update: bool = False
