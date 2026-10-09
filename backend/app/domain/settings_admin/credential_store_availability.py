"""M9 domain contract."""

from app.domain.domain_enum import DomainEnum


class CredentialStoreAvailability(DomainEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
