from app.domain.common import DomainEnum


class HistoricalMatchReason(DomainEnum):
    SAME_COMID = "same_comid"
    NEARBY_COMID = "nearby_comid"
    PARAMETER_OVERLAP = "parameter_overlap"
    WINDOW_OVERLAP = "window_overlap"
    SEASON_MATCH = "season_match"
    VECTOR_SIMILARITY = "vector_similarity"
    LEXICAL_SIMILARITY = "lexical_similarity"
    TOPOLOGY_PROXIMITY = "topology_proximity"
