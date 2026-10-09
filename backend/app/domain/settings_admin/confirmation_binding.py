"""M9 domain contract."""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from hmac import compare_digest
from app.domain.common import UtcDatetime, normalize_utc_datetime
from .admin_action_kind import AdminActionKind


@dataclass(frozen=True, slots=True, repr=False)
class ConfirmationBinding:
    token_digest: bytes
    action: AdminActionKind
    fact_fingerprint: bytes
    required_phrase: str
    expires_at: UtcDatetime

    def __post_init__(self) -> None:
        if len(self.token_digest) != 32 or len(self.fact_fingerprint) != 32:
            raise ValueError("confirmation digests must be SHA-256")
        if not self.required_phrase:
            raise ValueError("confirmation phrase is required")
        object.__setattr__(self, "expires_at", normalize_utc_datetime(self.expires_at))

    def matches(
        self, *, token: str, action: AdminActionKind, fact_fingerprint: bytes,
        phrase: str, now: datetime,
    ) -> bool:
        return (
            normalize_utc_datetime(now) < self.expires_at
            and action is self.action
            and compare_digest(self.fact_fingerprint, fact_fingerprint)
            and compare_digest(self.token_digest, sha256(token.encode("utf-8")).digest())
            and compare_digest(self.required_phrase, phrase)
        )
