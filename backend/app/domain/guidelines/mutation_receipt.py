from typing import Literal
from uuid import UUID

from app.domain.domain_model import DomainModel
from app.domain.guidelines.canonical import Digest, Revision
from app.domain.guidelines.mutation import GuidelineMutation


class GuidelineMutationReceipt(DomainModel):
    mutation: GuidelineMutation
    status_code: Literal[200, 201, 202]
    operation_id: UUID
    document_id: UUID | None = None
    version_id: UUID | None = None
    accepted_revision: Revision | None = None
    content_digest: Digest | None = None
