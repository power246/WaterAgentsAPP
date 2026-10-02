from app.domain.common import DomainModel, NonEmptyString


class ItemReference(DomainModel):
    item_id: NonEmptyString
