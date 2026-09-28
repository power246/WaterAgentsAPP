from app.domain.common import DomainEnum


class ExclusionReasonCode(DomainEnum):
    NO_QUALIFYING_SOURCE = "no_qualifying_source"
    SEARCH_LIMIT_REACHED = "search_limit_reached"
