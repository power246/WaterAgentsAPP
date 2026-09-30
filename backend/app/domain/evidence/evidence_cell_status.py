from app.domain.domain_enum import DomainEnum


class EvidenceCellStatus(DomainEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"
