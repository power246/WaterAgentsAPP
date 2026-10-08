from typing import Annotated

from pydantic import Field, model_validator

from app.domain.model_role import ModelRole
from app.domain.model_selection import ModelSelection
from app.domain.memory_rag import EmbeddingProfile


class ModelCapability(ModelSelection):
    roles: tuple[ModelRole, ...]
    context_window_tokens: Annotated[int, Field(strict=True, ge=1)] | None = None
    max_output_tokens: Annotated[int, Field(strict=True, ge=1)] | None = None
    embedding_profile: EmbeddingProfile | None = None

    @model_validator(mode="after")
    def validate_roles(self) -> "ModelCapability":
        if not self.roles:
            raise ValueError("roles must not be empty")
        if len(self.roles) != len(set(self.roles)):
            raise ValueError("roles must not contain duplicates")
        if ModelRole.EMBEDDING in self.roles:
            if self.roles != (ModelRole.EMBEDDING,):
                raise ValueError("embedding model role must be exclusive")
            if self.embedding_profile is None:
                raise ValueError("embedding model requires an embedding profile")
            if (
                self.embedding_profile.provider != self.provider
                or self.embedding_profile.model != self.model
            ):
                raise ValueError("embedding profile identity must match model capability")
        elif self.embedding_profile is not None:
            raise ValueError("embedding profile is only valid for embedding models")
        limits = (self.context_window_tokens, self.max_output_tokens)
        if ModelRole.WATERBODY in self.roles:
            if any(value is None for value in limits):
                raise ValueError("waterbody models require explicit token limits")
            if self.max_output_tokens > self.context_window_tokens:
                raise ValueError("max_output_tokens must not exceed context_window_tokens")
        elif any(value is not None for value in limits):
            raise ValueError("token limits are only valid for waterbody models")
        return self
