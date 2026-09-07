from app.domain.common import DomainEnum


class TimeResolutionSource(DomainEnum):
    FRONTEND = "frontend"
    DEFAULT = "default"
    AUTO = "auto"
