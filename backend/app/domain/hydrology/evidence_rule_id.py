from app.domain.domain_enum import DomainEnum


class EvidenceRuleId(DomainEnum):
    SAME_PARAMETER_UPSTREAM = "same_parameter_upstream"
    SAME_LOCATION_ADJACENT_TIME = "same_location_adjacent_time"
    TURBIDITY_PRECIPITATION_DISCHARGE_DIRECTIONAL = (
        "turbidity_precipitation_discharge_directional"
    )
    DISCHARGE_GAGE_HEIGHT_ESTIMATE = "discharge_gage_height_estimate"
    DISCHARGE_BASIN_ESTIMATE = "discharge_basin_estimate"
