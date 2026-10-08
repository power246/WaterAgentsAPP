"""M9 domain contract."""

from app.domain.domain_enum import DomainEnum


class CredentialName(DomainEnum):
    OPENAI = "openai"
    GOOGLE_GEOCODING = "google_geocoding"
