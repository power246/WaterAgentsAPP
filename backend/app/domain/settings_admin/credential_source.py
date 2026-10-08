"""M9 domain contract."""

from app.domain.domain_enum import DomainEnum


class CredentialSource(DomainEnum):
    USER_STORE = "user_store"
    DEPLOYMENT = "deployment"
    NONE = "none"
