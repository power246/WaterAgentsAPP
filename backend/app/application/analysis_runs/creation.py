from enum import Enum
from uuid import UUID

from pydantic import model_validator

from app.application.analysis_runs.models import AnalysisRunSnapshot
from app.application.transcripts.models import TranscriptStorageBatch
from app.domain.common import JsonValue
from app.domain.domain_model import DomainModel


class RunCreationCandidate(DomainModel):
    snapshot: AnalysisRunSnapshot
    query: str
    idempotency_key: UUID | None = None
    request_fingerprint: bytes | None = None
    creation_receipt: dict[str, JsonValue]
    request_item_id: UUID
    created_item_id: UUID
    transcript: TranscriptStorageBatch

    @model_validator(mode="after")
    def validate_creation(self) -> "RunCreationCandidate":
        if not self.query.strip():
            raise ValueError("query must not be blank")
        if (self.idempotency_key is None) != (self.request_fingerprint is None):
            raise ValueError("idempotency key and fingerprint must be provided together")
        if self.idempotency_key is not None and self.idempotency_key.version != 4:
            raise ValueError("idempotency key must be UUID v4")
        if self.request_fingerprint is not None and len(self.request_fingerprint) != 32:
            raise ValueError("request_fingerprint must contain a SHA-256 digest")
        items = {item.item_id: item for item in self.transcript.items}
        request = items.get(self.request_item_id)
        created = items.get(self.created_item_id)
        if request is None or created is None or len(items) != 2:
            raise ValueError("creation transcript must contain exactly its two root items")
        if (
            request.run_id != self.snapshot.run_id
            or request.session_id is not None
            or request.transcript_sequence != 1
            or request.content_type != "analysis_request"
            or request.content_schema_version != 1
        ):
            raise ValueError("request item must be the first run-owned analysis request")
        if (
            created.run_id != self.snapshot.run_id
            or created.session_id is not None
            or created.transcript_sequence != 2
            or created.content_type != "lifecycle_event"
            or created.content_schema_version != 2
        ):
            raise ValueError("created item must be the second run-owned lifecycle event")
        if self.snapshot.status.value != "pending":
            raise ValueError("new run creation must start pending")
        if AnalysisRunSnapshot.model_validate(self.creation_receipt) != self.snapshot:
            raise ValueError("creation receipt must be the immutable pending snapshot")
        return self


class StoredRunCreation(DomainModel):
    run_id: UUID
    idempotency_key: UUID | None = None
    request_fingerprint: bytes | None = None
    creation_receipt: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_idempotency(self) -> "StoredRunCreation":
        if (self.idempotency_key is None) != (self.request_fingerprint is None):
            raise ValueError("idempotency key and fingerprint must be provided together")
        if self.idempotency_key is not None and self.idempotency_key.version != 4:
            raise ValueError("idempotency key must be UUID v4")
        if self.request_fingerprint is not None and len(self.request_fingerprint) != 32:
            raise ValueError("request_fingerprint must contain a SHA-256 digest")
        return self

    def receipt_snapshot(self) -> AnalysisRunSnapshot:
        snapshot = AnalysisRunSnapshot.model_validate(self.creation_receipt)
        if snapshot.run_id != self.run_id:
            raise ValueError("creation receipt run identity does not match its row")
        return snapshot


class RunCreationDisposition(str, Enum):
    CREATED = "created"
    EXISTING = "existing"


class RunCreationResult(DomainModel):
    disposition: RunCreationDisposition
    creation: StoredRunCreation


class IdempotencyKeyConflictError(Exception):
    pass


class InvalidIdempotencyKeyError(ValueError):
    pass


class InvalidSubmissionEncodingError(ValueError):
    pass


class IdempotencyWaitTimeoutError(TimeoutError):
    pass


class CreationCommitUnconfirmedError(TimeoutError):
    def __init__(self, run_id: UUID) -> None:
        self.run_id = run_id
        super().__init__("The run creation commit has not been confirmed.")


class CreationPersistenceUnavailableError(RuntimeError):
    pass


class CreationVerificationUnavailableError(RuntimeError):
    pass


class CreationRolledBackError(RuntimeError):
    pass
