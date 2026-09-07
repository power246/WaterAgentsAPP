from app.domain.common import DomainEnum


class AttributionBasis(DomainEnum):
    OBSERVED = "observed"
    CORRELATED = "correlated"
    INFERRED = "inferred"
    HYPOTHESIZED = "hypothesized"
