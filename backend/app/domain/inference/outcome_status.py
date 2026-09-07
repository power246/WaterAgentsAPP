from app.domain.common import DomainEnum


class OutcomeStatus(DomainEnum):
    QUANTIFIED = "quantified"
    DIRECTIONAL = "directional"
    INDETERMINATE = "indeterminate"
