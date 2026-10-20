from app.domain.common import DomainEnum


class OutcomeStatus(DomainEnum):
    QUANTIFIED = "quantified"
    RANGE_ESTIMATE = "range_estimate"
    DIRECTIONAL = "directional"
    INDETERMINATE = "indeterminate"
