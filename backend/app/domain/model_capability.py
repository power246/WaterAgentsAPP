from pydantic import model_validator

from app.domain.model_role import ModelRole
from app.domain.model_selection import ModelSelection


class ModelCapability(ModelSelection):
    roles: tuple[ModelRole, ...]

    @model_validator(mode="after")
    def validate_roles(self) -> "ModelCapability":
        if not self.roles:
            raise ValueError("roles must not be empty")
        if len(self.roles) != len(set(self.roles)):
            raise ValueError("roles must not contain duplicates")
        return self
