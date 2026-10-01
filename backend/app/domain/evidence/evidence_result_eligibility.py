from app.domain.domain_enum import DomainEnum


class EvidenceResultEligibility(DomainEnum):
    OBSERVED_OR_INFERRED = "observed_or_inferred"
    INFERRED_ONLY = "inferred_only"
    EXPLANATORY_ONLY = "explanatory_only"
