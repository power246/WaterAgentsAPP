from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from app.domain.common import (
    DomainModel,
    NonEmptyString,
    UtcDatetime,
    ensure_unique_tuple,
)
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.inference.mode import InferenceMode
from app.domain.memory_rag.embedding_vector import EmbeddingVector
from app.domain.memory_rag.historical_case_ref import HistoricalCaseRef
from app.domain.memory_rag.historical_result_projection import (
    HistoricalResultProjection,
)


class RAGCaseProjectionV1(DomainModel):
    case_ref: HistoricalCaseRef
    profile_id: NonEmptyString
    projection_schema_version: Literal[1] = 1
    source_content_type: Literal["inference_result"] = "inference_result"
    source_content_schema_version: Literal[1] = 1
    mode: InferenceMode
    parameter_keys: Annotated[tuple[ParameterKey, ...], Field(min_length=1)]
    window_ids: Annotated[tuple[NonEmptyString, ...], Field(min_length=1)]
    source_completed_at: UtcDatetime
    canonical_query: Annotated[NonEmptyString, Field(max_length=16_384)]
    canonical_query_digest: NonEmptyString
    result_projection: HistoricalResultProjection
    projection_digest: NonEmptyString
    embedding: EmbeddingVector

    @field_validator("parameter_keys", "window_ids")
    @classmethod
    def validate_unique_projection_keys(
        cls,
        value: tuple[object, ...],
    ) -> tuple[object, ...]:
        return ensure_unique_tuple(value, field_name="RAG case projection keys")

    @model_validator(mode="after")
    def validate_safe_projection_scope(self) -> Self:
        cells = self.result_projection.cells
        if not {cell.parameter for cell in cells} <= set(self.parameter_keys):
            raise ValueError("result projection parameter is outside case projection")
        if not {cell.window_id for cell in cells} <= set(self.window_ids):
            raise ValueError("result projection window is outside case projection")
        if self.result_projection.source_completed_at != self.source_completed_at:
            raise ValueError("result and case completion times must match")
        return self
