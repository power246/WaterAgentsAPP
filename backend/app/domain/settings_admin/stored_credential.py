"""M9 domain contract."""

from dataclasses import dataclass
from pydantic import SecretStr
from app.domain.common import UtcDatetime, normalize_utc_datetime


@dataclass(frozen=True, slots=True, repr=False)
class StoredCredential:
    value: SecretStr
    updated_at: UtcDatetime

    def __post_init__(self) -> None:
        if not isinstance(self.value, SecretStr) or not self.value.get_secret_value().strip():
            raise ValueError("stored credential must contain a nonempty secret")
        object.__setattr__(self, "updated_at", normalize_utc_datetime(self.updated_at))
