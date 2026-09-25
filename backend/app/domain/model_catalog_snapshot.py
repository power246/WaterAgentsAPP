from pydantic import model_validator

from app.domain.domain_model import DomainModel
from app.domain.model_capability import ModelCapability
from app.domain.model_role import ModelRole
from app.domain.model_selection import ModelSelection


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
        return self

    def supports(self, selection: ModelSelection, role: ModelRole) -> bool:
        return any(
            capability.provider == selection.provider
            and capability.model == selection.model
            and role in capability.roles
            for capability in self.capabilities
        )
