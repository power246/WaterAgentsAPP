from app.domain.common import DomainEnum


class AssessmentAvailability(DomainEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    INDETERMINATE = "indeterminate"
