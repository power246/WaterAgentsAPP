from pydantic import UUID4

from app.domain.domain_model import DomainModel
from app.domain.guidelines.canonical import Digest
from app.domain.guidelines.operation_kind import GuidelineOperationKind


class GuidelineMutation(DomainModel):
    idempotency_key: UUID4
    kind: GuidelineOperationKind
    request_fingerprint: Digest
