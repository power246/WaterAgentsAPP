"""M9 domain contract."""

from dataclasses import dataclass
from pydantic import SecretStr
from app.domain.common import UtcDatetime, normalize_utc_datetime
from .credential_name import CredentialName
from .credential_source import CredentialSource


@dataclass(frozen=True, slots=True, repr=False)
class CredentialLease:
    name: CredentialName
    value: SecretStr
    source: CredentialSource
    issued_at: UtcDatetime

    def __post_init__(self) -> None:
        if not isinstance(self.value, SecretStr) or not self.value.get_secret_value().strip():
            raise ValueError("credential lease must contain a nonempty secret")
        if self.source is CredentialSource.NONE:
            raise ValueError("credential lease requires an effective source")
        object.__setattr__(self, "issued_at", normalize_utc_datetime(self.issued_at))
