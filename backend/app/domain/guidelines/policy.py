from typing import Annotated, Literal

from pydantic import Field, field_validator

from app.domain.common import DomainFloat
from app.domain.domain_model import DomainModel
from app.domain.guidelines.canonical import NormalizedText, require_safe_model_text
from app.domain.guidelines.canonical_query import MAX_GUIDELINE_QUERY_BYTES


class GuidelinePolicy(DomainModel):
    version: Literal[1] = 1
    top_k: Annotated[int, Field(strict=True, ge=1, le=3)] = 3
    semantic_min_similarity: Annotated[DomainFloat, Field(ge=0, le=1)] = 0.35
    knowledge_budget_tokens: Annotated[int, Field(strict=True, ge=1, le=3072)] = 3072
    max_query_bytes: Literal[8192] = MAX_GUIDELINE_QUERY_BYTES
    online_timeout_seconds: Annotated[DomainFloat, Field(gt=0, le=3)] = 3.0
    online_attempts: Literal[1] = 1
    worker_max_concurrency: Annotated[int, Field(strict=True, ge=1, le=8)] = 2
    worker_timeout_seconds: Annotated[DomainFloat, Field(gt=0, le=120)] = 20.0
    max_attempts: Literal[3] = 3
    retry_backoff_seconds: tuple[Literal[2], Literal[10]] = (2, 10)
    document_instructions: Annotated[NormalizedText, Field(min_length=1, max_length=2000)] = (
        "Represent this water-quality advisory title, summary, parameters, scenarios, and keywords for retrieval."
    )
    query_instructions: Annotated[NormalizedText, Field(min_length=1, max_length=2000)] = (
        "Retrieve water-quality advisory topics relevant to the question, parameters, and known task context."
    )

    @field_validator("version", "max_query_bytes", "online_attempts", "max_attempts", mode="before")
    @classmethod
    def integer_contract(cls, value):
        if type(value) is not int:
            raise ValueError("fixed policy counts must be integers")
        return value

    @field_validator("retry_backoff_seconds", mode="before")
    @classmethod
    def integer_backoff(cls, value):
        if not isinstance(value, (list, tuple)) or any(type(item) is not int for item in value):
            raise ValueError("retry delays must be integers")
        return value

    @field_validator("document_instructions", "query_instructions")
    @classmethod
    def safe_instructions(cls, value):
        if not value.strip():
            raise ValueError("embedding role instruction must not be blank")
        require_safe_model_text(value)
        return value


DEFAULT_GUIDELINE_POLICY = GuidelinePolicy()
