from app.domain.common import DomainEnum


class ResultSource(DomainEnum):
    OBSERVED = "observed"
    INFERRED = "inferred"
    PREDICTED = "predicted"
