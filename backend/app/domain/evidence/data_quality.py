from app.domain.common import DomainEnum


class DataQualityFlag(DomainEnum):
    MISSING = "missing"
    PARTIAL_COVERAGE = "partial_coverage"
    POSSIBLE_FREEZING = "possible_freezing"
    ANOMALOUS = "anomalous"
    ESTIMATED = "estimated"
    HISTORICAL_INFERENCE = "historical_inference"
    STALE = "stale"
    PROVIDER_FLAGGED = "provider_flagged"
    UNIT_UNRESOLVED = "unit_unresolved"
    CONFLICTING_OBSERVATIONS = "conflicting_observations"
