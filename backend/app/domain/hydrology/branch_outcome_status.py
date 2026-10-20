from app.domain.common import DomainEnum


class BranchOutcomeStatus(DomainEnum):
    QUALIFIED = "qualified"
    NO_QUALIFYING_SOURCE = "no_qualifying_source"
    SEARCH_LIMIT_REACHED = "search_limit_reached"
    BACKUP_SEARCH_ENDED = "backup_search_ended"
