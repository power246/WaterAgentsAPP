import json
from hashlib import sha256
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
from app.domain.memory_rag.canonical_case_query import CanonicalCaseQueryV1
from app.domain.memory_rag.embedding_vector import EmbeddingVector
from app.domain.memory_rag.historical_case_ref import HistoricalCaseRef
from app.domain.memory_rag.historical_result_projection import (
    HistoricalResultProjection,
)
from app.domain.memory_rag.rag_case_window import RAGCaseWindowV1

_AUTO_DIGEST = "__auto_rag_case_projection_digest__"
_AUTO_CANONICAL_DIGEST = "__auto_canonical_query_digest__"


class PreparedRAGCaseProjectionV1(DomainModel):
    case_ref: HistoricalCaseRef
    profile_id: NonEmptyString
    projection_schema_version: Literal[1] = 1
    source_content_type: Literal["inference_result"] = "inference_result"
    source_content_schema_version: Literal[1] = 1
    mode: InferenceMode
    parameter_keys: Annotated[tuple[ParameterKey, ...], Field(min_length=1)]
    window_ids: Annotated[tuple[NonEmptyString, ...], Field(min_length=1)]
    windows: Annotated[
        tuple[RAGCaseWindowV1, ...],
        Field(min_length=1, max_length=60),
    ]
    source_completed_at: UtcDatetime
    canonical_query: Annotated[NonEmptyString, Field(max_length=16_384)]
    canonical_query_digest: NonEmptyString = _AUTO_CANONICAL_DIGEST
    result_projection: HistoricalResultProjection
    projection_digest: NonEmptyString = _AUTO_DIGEST

    @field_validator("parameter_keys", "window_ids")
    @classmethod
    def validate_unique_projection_keys(
        cls,
        value: tuple[object, ...],
    ) -> tuple[object, ...]:
        ensure_unique_tuple(value, field_name="RAG case projection keys")
        if value and isinstance(value[0], ParameterKey):
            return tuple(
                sorted(
                    value,
                    key=lambda item: (item.code_system, item.code),
                )
            )
        return value

    @model_validator(mode="after")
    def validate_safe_projection_scope(self) -> Self:
        cells = self.result_projection.cells
        if not {cell.parameter for cell in cells} <= set(self.parameter_keys):
            raise ValueError("result projection parameter is outside case projection")
        if not {cell.window_id for cell in cells} <= set(self.window_ids):
            raise ValueError("result projection window is outside case projection")
        if self.result_projection.source_completed_at != self.source_completed_at:
            raise ValueError("result and case completion times must match")
        if self.window_ids != tuple(window.window_id for window in self.windows):
            raise ValueError("case window_ids must match ordered window projections")
        if any(window.parameter_keys != self.parameter_keys for window in self.windows):
            raise ValueError("case window parameters must match case parameters")
        windows_by_id = {window.window_id: window for window in self.windows}
        if len(windows_by_id) != len(self.windows):
            raise ValueError("case windows must not contain duplicate identities")
        if any(
            cell.time_range != windows_by_id[cell.window_id].time_range
            for cell in cells
        ):
            raise ValueError("result cells must match their case window time ranges")
        canonical = CanonicalCaseQueryV1.model_validate_json(self.canonical_query)
        if canonical.canonical_text != self.canonical_query:
            raise ValueError("canonical query must use the frozen V1 serialization")
        projected_windows = CanonicalCaseQueryV1(
            mode=self.mode,
            requested_parameter_keys=self.parameter_keys,
            supporting_parameter_keys=canonical.supporting_parameter_keys,
            window_descriptors=tuple(
                window.canonical_descriptor() for window in self.windows
            ),
            normalized_intent_tokens=canonical.normalized_intent_tokens,
        ).window_descriptors
        if (
            canonical.mode is not self.mode
            or canonical.requested_parameter_keys != self.parameter_keys
            or canonical.window_descriptors != projected_windows
        ):
            raise ValueError("canonical query must match the case projection")
        expected_canonical = (
            f"sha256:{sha256(self.canonical_query.encode('utf-8')).hexdigest()}"
        )
        if self.canonical_query_digest == _AUTO_CANONICAL_DIGEST:
            object.__setattr__(self, "canonical_query_digest", expected_canonical)
        elif self.canonical_query_digest != expected_canonical:
            raise ValueError("canonical query digest does not match canonical bytes")
        projection_values = self.model_dump(
            mode="json",
            exclude={"embedding", "projection_digest"},
        )
        expected_projection = f"sha256:{sha256(json.dumps(
            projection_values,
            allow_nan=False,
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
        ).encode('utf-8')).hexdigest()}"
        if self.projection_digest == _AUTO_DIGEST:
            object.__setattr__(self, "projection_digest", expected_projection)
        elif self.projection_digest != expected_projection:
            raise ValueError("case projection digest does not match canonical content")
        return self

    def with_embedding(self, embedding: EmbeddingVector) -> "RAGCaseProjectionV1":
        from app.domain.memory_rag.rag_case_projection import RAGCaseProjectionV1

        return RAGCaseProjectionV1(
            **self.model_dump(mode="python"),
            embedding=embedding,
        )
