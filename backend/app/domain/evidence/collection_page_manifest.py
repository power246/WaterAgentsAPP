from typing import Annotated

from pydantic import Field

from app.domain.common import DomainModel, NonEmptyString


class CollectionPageManifest(DomainModel):
    page_identity: NonEmptyString
    record_count: Annotated[int, Field(strict=True, ge=0)]
    response_bytes: Annotated[int, Field(strict=True, ge=0)]
    page_complete: bool
    next_page_identity: NonEmptyString | None = None
