from app.domain.common import DomainEnum
from app.domain.hydrology.branch_outcome import BranchOutcome
from app.domain.hydrology.branch_outcome_status import BranchOutcomeStatus


class TopologyCoverage(DomainEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    NONE = "none"


def derive_topology_coverage(
    outcomes: tuple[BranchOutcome, ...],
) -> TopologyCoverage:
    qualified = tuple(
        item for item in outcomes if item.outcome is BranchOutcomeStatus.QUALIFIED
    )
    if not qualified:
        return TopologyCoverage.NONE
    if any(
        item.outcome
        in {
            BranchOutcomeStatus.NO_QUALIFYING_SOURCE,
            BranchOutcomeStatus.SEARCH_LIMIT_REACHED,
        }
        for item in outcomes
    ):
        return TopologyCoverage.PARTIAL
    return TopologyCoverage.COMPLETE
