from app.domain.common import DomainEnum


class HistoricalSearchStrategy(DomainEnum):
    EXACT_COMID_PARAMETER_WINDOW = "exact_comid_parameter_window"
    NEARBY_COMID_PARAMETER = "nearby_comid_parameter"
    PARAMETER_SEASON = "parameter_season"
    SEMANTIC_CASE_QUERY = "semantic_case_query"
    TOPOLOGY_NEIGHBORHOOD_PARAMETER = "topology_neighborhood_parameter"
