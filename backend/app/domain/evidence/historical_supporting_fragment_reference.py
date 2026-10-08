from app.domain.common import DomainModel, NonEmptyString


class HistoricalSupportingFragmentReference(DomainModel):
    item_id: NonEmptyString
    evidence_id: NonEmptyString
