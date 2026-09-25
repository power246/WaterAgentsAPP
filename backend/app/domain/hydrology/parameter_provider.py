from app.domain.domain_enum import DomainEnum


class ParameterProvider(DomainEnum):
    WDFN_TIME_SERIES = "wdfn_time_series"
    USGS_SAMPLES = "usgs_samples"
