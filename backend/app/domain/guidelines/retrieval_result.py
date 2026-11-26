from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.domain.domain_model import DomainModel
from app.domain.guidelines.canonical import Digest
from app.domain.guidelines.match import GuidelineMatch
from app.domain.guidelines.run_pin import SafeReason


class GuidelineRetrievalResult(DomainModel):
    status: Literal["retrieved", "empty_library", "no_match", "unavailable", "budget_limited", "cancelled"]
    query_digest: Digest | None = None
    input_reduced: bool = False
    matches: Annotated[tuple[GuidelineMatch, ...], Field(max_length=3)] = ()
    safe_error_code: SafeReason | None = None

    @model_validator(mode="after")
    def consistent_selection(self) -> Self:
        if bool(self.matches) != (self.status == "retrieved"):
            raise ValueError("only a successful retrieval has selected guidelines")
        roots = tuple(match.version.identity.family_root_id for match in self.matches)
        if len(roots) != len(set(roots)):
            raise ValueError("retrieval must select each family at most once")
        if self.matches != tuple(sorted(self.matches, key=lambda match: (
            -match.similarity, match.version.identity.family_root_id, match.version.version_id,
        ))):
            raise ValueError("guideline ranking must be stable")
        return self
