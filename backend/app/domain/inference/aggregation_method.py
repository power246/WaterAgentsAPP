from app.domain.common import DomainEnum


class AggregationMethod(DomainEnum):
    MEAN = "mean"
    MEDIAN = "median"
    MIN = "min"
    MAX = "max"
    SUM = "sum"
    P90 = "p90"
    LATEST = "latest"
