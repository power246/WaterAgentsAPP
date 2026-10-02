from app.domain.common import DomainModel, NonEmptyString
from app.domain.evidence.collection_completion_contract import (
    CollectionCompletionContract,
)


class CollectionCompletionProof(DomainModel):
    contract: CollectionCompletionContract
    complete: bool
    terminal_identity: NonEmptyString | None = None
    digest: NonEmptyString
