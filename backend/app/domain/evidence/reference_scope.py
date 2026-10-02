from app.domain.common import DomainModel, NonEmptyString


class ReferenceScope(DomainModel):
    item_id: NonEmptyString
    run_id: NonEmptyString
    session_id: NonEmptyString | None = None
