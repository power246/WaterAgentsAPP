from app.domain.domain_enum import DomainEnum


class GuidelineSourceKind(DomainEnum):
    REFERENCE_DERIVED = "reference_derived"
    USER_AUTHORED = "user_authored"
