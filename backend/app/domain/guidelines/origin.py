from app.domain.domain_enum import DomainEnum


class GuidelineOrigin(DomainEnum):
    BUNDLED = "bundled"
    LOCAL_CUSTOM = "local_custom"
    LOCAL_OVERRIDE = "local_override"
