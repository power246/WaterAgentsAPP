from app.domain.domain_enum import DomainEnum


class ValueSemantics(DomainEnum):
    INSTANTANEOUS = "instantaneous"
    INTERVAL_MEAN = "interval_mean"
    INTERVAL_MIN = "interval_min"
    INTERVAL_MAX = "interval_max"
    INTERVAL_TOTAL = "interval_total"
    CUMULATIVE_COUNTER = "cumulative_counter"
    CATEGORICAL_STATE = "categorical_state"
    FORECAST_INTERVAL = "forecast_interval"
