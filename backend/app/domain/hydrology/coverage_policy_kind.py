from app.domain.domain_enum import DomainEnum


class CoveragePolicyKind(DomainEnum):
    CONTINUOUS_REGULAR = "continuous_regular"
    CONTINUOUS_LATEST = "continuous_latest"
    DISCRETE_SAMPLE = "discrete_sample"
    PROVIDER_INTERVAL = "provider_interval"
    FORECAST_INTERVAL = "forecast_interval"
