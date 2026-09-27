from app.domain.common import DomainEnum


class BranchOutcomeReason(DomainEnum):
    TARGET_STATION_QUALIFIED = "target_station_qualified"
    STATION_QUALIFIED = "station_qualified"
    NATURAL_HEADWATER = "natural_headwater"
    MAX_DISTANCE_REACHED = "max_distance_reached"
    MAX_HOPS_REACHED = "max_hops_reached"
    MAX_NODES_REACHED = "max_nodes_reached"
    MULTIPLE_LIMITS_REACHED = "multiple_limits_reached"
