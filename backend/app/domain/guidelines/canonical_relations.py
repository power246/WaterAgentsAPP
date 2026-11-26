import re
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.domain.domain_model import DomainModel
from app.domain.guidelines.canonical import Digest, NormalizedText, canonical_text, digest
from app.domain.guidelines.canonical_fragment import CanonicalGuidelineFragmentV3
from app.domain.guidelines.content import GuidelineContentV1
from app.domain.guidelines.embedding_profile import GuidelineEmbeddingProfileV1
from app.domain.guidelines.model_view import project_model_view
from app.domain.hydrology.parameter_capability_registry import ParameterCapabilityRegistry


class CanonicalGuidelineDocumentV3(DomainModel):
    kind: Literal["guideline_document"] = "guideline_document"
    schema_version: Literal[3] = 3
    template_version: Literal[3] = 3
    profile_id: Annotated[str, Field(pattern=r"^guideline:sha256:[0-9a-f]{64}$")]
    title: Annotated[NormalizedText, Field(min_length=1, max_length=160)]
    summary: Annotated[NormalizedText, Field(min_length=1, max_length=240)]
    body_digest: Digest
    fragments: Annotated[tuple[CanonicalGuidelineFragmentV3, ...], Field(min_length=1, max_length=128)]
    links: Annotated[tuple[tuple[Digest, Annotated[str, Field(pattern=r"^(paragraph:[0-9]+|summary)$")]], ...], Field(min_length=1, max_length=128)]

    @model_validator(mode="after")
    def complete_manifest(self) -> Self:
        ids = tuple(fragment.canonical_digest for fragment in self.fragments)
        if ids != tuple(sorted(set(ids))) or self.links != tuple(sorted(set(self.links))):
            raise ValueError("relation manifest must be canonical and unique")
        if {key for key, _ in self.links} != set(ids) or any(f.profile_id != self.profile_id for f in self.fragments):
            raise ValueError("relation links must cover exactly this profile's fragments")
        return self

    @property
    def canonical_text(self) -> str:
        return canonical_text(self.model_dump(mode="json"))

    @property
    def canonical_digest(self) -> str:
        return digest(self.model_dump(mode="json"))

    @classmethod
    def from_content(cls, content: GuidelineContentV1, profile: GuidelineEmbeddingProfileV1,
                     registry: ParameterCapabilityRegistry) -> Self:
        content.require_registered_parameters(registry)
        view = project_model_view(content)
        fragments, links = {}, []
        heading_path = []
        for index, paragraph in enumerate(re.split(r"\n\s*\n|(?m:\n(?=[ \t]*#{1,6}[ \t]+))", view.body)):
            for marks, heading in re.findall(r"(?m)^\s*(#{1,6})\s+([^\n]+)", paragraph):
                level = len(marks)
                heading_path = [(depth, label) for depth, label in heading_path if depth < level]
                heading_path.append((level, heading.strip()))
            relation = re.sub(r"(?m)^\s*#{1,6}\s+[^\n]*(?:\n|$)", "", paragraph).strip()
            relation = re.sub(r"\[source:[^\]]+\]", "", relation).strip()
            if not relation:
                continue
            if heading_path:
                relation = " / ".join(label for _, label in heading_path)+"\n"+relation
            fragment = CanonicalGuidelineFragmentV3(profile_id=profile.profile_id,
                instructions=profile.document_instructions, relation=relation)
            fragments[fragment.canonical_digest] = fragment
            links.append((fragment.canonical_digest, f"paragraph:{index}"))
        if not fragments:
            fragment = CanonicalGuidelineFragmentV3(profile_id=profile.profile_id,
                instructions=profile.document_instructions, relation=view.summary)
            fragments[fragment.canonical_digest] = fragment
            links.append((fragment.canonical_digest, "summary"))
        return cls(profile_id=profile.profile_id, title=view.title, summary=view.summary,
            body_digest=digest(view.body), fragments=tuple(fragments[key] for key in sorted(fragments)),
            links=tuple(sorted(links)))
