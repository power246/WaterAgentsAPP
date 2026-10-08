from pydantic import model_validator

from app.domain.domain_model import DomainModel
from app.domain.model_capability import ModelCapability
from app.domain.model_role import ModelRole
from app.domain.model_selection import ModelSelection
from app.domain.memory_rag import EmbeddingProfile


class ModelCatalogSnapshot(DomainModel):
    capabilities: tuple[ModelCapability, ...]

    @model_validator(mode="after")
    def validate_capabilities(self) -> "ModelCatalogSnapshot":
        identities = [
            (capability.provider, capability.model)
            for capability in self.capabilities
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("model catalog identities must not contain duplicates")
        embedding_capabilities = tuple(
            capability
            for capability in self.capabilities
            if ModelRole.EMBEDDING in capability.roles
        )
        if self.capabilities and not embedding_capabilities:
            raise ValueError("model catalog requires an embedding capability")
        return self

    @property
    def embedding_profile(self) -> EmbeddingProfile:
        capability = next(
            item
            for item in self.capabilities
            if ModelRole.EMBEDDING in item.roles
        )
        assert capability.embedding_profile is not None
        return capability.embedding_profile

    def supports(self, selection: ModelSelection, role: ModelRole) -> bool:
        return self.capability_for(selection, role) is not None

    def capability_for(
        self,
        selection: ModelSelection,
        role: ModelRole,
    ) -> ModelCapability | None:
        return next(
            (
                capability
                for capability in self.capabilities
                if capability.provider == selection.provider
                and capability.model == selection.model
                and role in capability.roles
            ),
            None,
        )
