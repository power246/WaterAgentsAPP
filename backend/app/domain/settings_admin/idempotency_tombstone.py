"""M9 domain contract."""

from dataclasses import dataclass
from uuid import UUID
from app.domain.common import UtcDatetime, normalize_utc_datetime


@dataclass(frozen=True, slots=True, repr=False)
class IdempotencyTombstone:
    idempotency_key: UUID
    request_fingerprint: bytes
    deleted_run_id: UUID
    deleted_at: UtcDatetime

    def __post_init__(self) -> None:
        if self.idempotency_key.version != 4:
            raise ValueError("idempotency key must be UUID v4")
        if len(self.request_fingerprint) != 32:
            raise ValueError("request fingerprint must be SHA-256")
        object.__setattr__(self, "deleted_at", normalize_utc_datetime(self.deleted_at))
