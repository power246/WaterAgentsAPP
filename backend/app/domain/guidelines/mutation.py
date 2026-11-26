from pydantic import UUID4

from app.domain.domain_model import DomainModel
from app.domain.guidelines.canonical import Digest, digest
from app.domain.guidelines.operation_kind import GuidelineOperationKind


class GuidelineMutation(DomainModel):
    idempotency_key: UUID4
    kind: GuidelineOperationKind
    request_fingerprint: Digest

    @classmethod
    def for_request(cls, idempotency_key, kind: GuidelineOperationKind, body: dict):
        """Body contains canonical domain JSON, including expected revisions.

        Server-assigned operation/version IDs and review timestamps are excluded.
        """
        return cls(idempotency_key=idempotency_key, kind=kind,
                   request_fingerprint=digest({"kind": kind, "body": body}))

    def require_request(self, kind: GuidelineOperationKind, body: dict) -> None:
        expected = self.for_request(self.idempotency_key, kind, body)
        if self != expected:
            raise ValueError("guideline mutation does not match its canonical request")
