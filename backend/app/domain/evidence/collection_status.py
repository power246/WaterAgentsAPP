from app.domain.common import DomainEnum


class CollectionStatus(DomainEnum):
    AVAILABLE = "available"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"
