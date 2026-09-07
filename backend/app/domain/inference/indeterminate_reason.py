from app.domain.common import DomainEnum


class IndeterminateReason(DomainEnum):
    NO_STATION_PATH = "no_station_path"
    PARAMETER_UNOBSERVED = "parameter_unobserved"
    INVALID_OBSERVATIONS = "invalid_observations"
    SOURCE_UNAVAILABLE = "source_unavailable"
    NO_APPLICABLE_EVIDENCE = "no_applicable_evidence"
