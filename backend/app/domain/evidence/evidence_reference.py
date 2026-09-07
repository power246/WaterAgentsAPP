from app.domain.common import DomainModel, NonEmptyString


class EvidenceReference(DomainModel):
    evidence_id: NonEmptyString
    role: NonEmptyString
    summary: NonEmptyString | None
