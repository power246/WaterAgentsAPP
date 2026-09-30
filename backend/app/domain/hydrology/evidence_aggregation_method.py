from app.domain.domain_enum import DomainEnum


class EvidenceAggregationMethod(DomainEnum):
    MEAN = "mean"
    MEDIAN = "median"
    MIN = "min"
    MAX = "max"
    SUM = "sum"
    P90 = "p90"
    LATEST = "latest"
    COUNTER_DELTA_SUM = "counter_delta_sum"
