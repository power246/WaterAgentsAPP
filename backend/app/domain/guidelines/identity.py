from typing import Annotated, Self
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, model_validator

from app.domain.domain_model import DomainModel
from app.domain.guidelines.canonical import Digest
from app.domain.guidelines.origin import GuidelineOrigin

BundleKey = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")]


def bundled_guideline_id(bundle_key: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"wateragents:guideline:bundle:{bundle_key}")


class GuidelineIdentity(DomainModel):
    guideline_id: UUID
    family_root_id: UUID
    origin: GuidelineOrigin
    bundle_key: BundleKey | None = None
    base_bundle_key: BundleKey | None = None
    based_on_version_id: UUID | None = None
    base_content_digest: Digest | None = None

    @model_validator(mode="after")
    def consistent_origin(self) -> Self:
        base_fields = (self.base_bundle_key, self.based_on_version_id, self.base_content_digest)
        if self.origin is GuidelineOrigin.BUNDLED:
            if self.bundle_key is None or any(value is not None for value in base_fields):
                raise ValueError("bundled identity requires only bundle_key")
            if self.guideline_id != bundled_guideline_id(self.bundle_key):
                raise ValueError("bundled identity must use its stable bundle UUID")
        elif self.guideline_id.version != 4 or self.bundle_key is not None:
            raise ValueError("local guideline identity must be UUIDv4 without bundle_key")
        if self.origin is GuidelineOrigin.LOCAL_OVERRIDE:
            if any(value is None for value in base_fields):
                raise ValueError("local override requires complete base provenance")
            if self.family_root_id != bundled_guideline_id(self.base_bundle_key):
                raise ValueError("override family must match its base bundle")
        elif self.family_root_id != self.guideline_id or any(value is not None for value in base_fields):
            raise ValueError("base/custom identity must be its own family without override provenance")
        return self
