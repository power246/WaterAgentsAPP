from app.domain.domain_enum import DomainEnum


class EvidenceApplicability(DomainEnum):
    DIRECT_MEASUREMENT = "direct_measurement"
    ALLOWED_INDIRECT = "allowed_indirect"
    CONTEXTUAL_ONLY = "contextual_only"
