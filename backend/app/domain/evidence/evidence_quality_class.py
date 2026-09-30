from app.domain.domain_enum import DomainEnum


class EvidenceQualityClass(DomainEnum):
    VALID = "valid"
    SUSPICIOUS = "suspicious"
    INVALID = "invalid"
