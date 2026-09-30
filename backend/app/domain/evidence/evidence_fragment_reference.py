from app.domain.common import DomainModel, NonEmptyString


class EvidenceFragmentReference(DomainModel):
    item_id: NonEmptyString
    evidence_id: NonEmptyString
