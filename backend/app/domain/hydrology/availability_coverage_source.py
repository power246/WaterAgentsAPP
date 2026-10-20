from app.domain.domain_enum import DomainEnum


class AvailabilityCoverageSource(DomainEnum):
    HISTORICAL_TIME_SERIES_ENVELOPE = "historical_time_series_envelope"
    HISTORICAL_SAMPLE_PERIOD = "historical_sample_period"
    HISTORICAL_TIME_SERIES_OBSERVATION = "historical_time_series_observation"
    HISTORICAL_SAMPLE_OBSERVATION = "historical_sample_observation"
    PREDICTION_TIME_SERIES_BASELINE = "prediction_time_series_baseline"
    PREDICTION_SAMPLE_BASELINE = "prediction_sample_baseline"
