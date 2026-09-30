from app.domain.common import DomainModel, NonEmptyString


class CollectionCompletionProof(DomainModel):
    contract: NonEmptyString
    complete: bool
    terminal_identity: NonEmptyString | None = None
    digest: NonEmptyString
