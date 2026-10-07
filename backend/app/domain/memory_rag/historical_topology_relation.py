from app.domain.common import DomainEnum


class HistoricalTopologyRelation(DomainEnum):
    ANCESTOR = "ancestor"
    DESCENDANT = "descendant"
