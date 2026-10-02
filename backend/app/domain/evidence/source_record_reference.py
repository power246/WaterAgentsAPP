from app.domain.common import DomainModel, NonEmptyString


class SourceRecordReference(DomainModel):
    item_id: NonEmptyString
    record_id: NonEmptyString
