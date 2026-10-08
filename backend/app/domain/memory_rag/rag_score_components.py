from typing import Annotated, Self

from pydantic import Field, model_validator

from app.domain.common import DomainFloat, DomainModel, ensure_unique_tuple
from app.domain.memory_rag.historical_match_reason import HistoricalMatchReason
from app.domain.memory_rag.historical_search_strategy import HistoricalSearchStrategy
from app.domain.memory_rag.historical_topology_relation import (
    HistoricalTopologyRelation,
)

_COMPONENT_REASON = {
    "same_comid": HistoricalMatchReason.SAME_COMID,
    "spatial_proximity": HistoricalMatchReason.NEARBY_COMID,
    "parameter_overlap_ratio": HistoricalMatchReason.PARAMETER_OVERLAP,
    "window_overlap_ratio": HistoricalMatchReason.WINDOW_OVERLAP,
    "season_similarity": HistoricalMatchReason.SEASON_MATCH,
    "vector_similarity": HistoricalMatchReason.VECTOR_SIMILARITY,
    "lexical_similarity": HistoricalMatchReason.LEXICAL_SIMILARITY,
    "topology_proximity": HistoricalMatchReason.TOPOLOGY_PROXIMITY,
}


class RAGScoreComponentsV1(DomainModel):
    strategy: HistoricalSearchStrategy
    reasons: Annotated[tuple[HistoricalMatchReason, ...], Field(min_length=1)]
    same_comid: bool | None = None
    parameter_overlap_ratio: Annotated[DomainFloat, Field(ge=0, le=1)] | None = None
    window_overlap_ratio: Annotated[DomainFloat, Field(ge=0, le=1)] | None = None
    spatial_proximity: Annotated[DomainFloat, Field(ge=0, le=1)] | None = None
    season_similarity: Annotated[DomainFloat, Field(ge=0, le=1)] | None = None
    duration_compatibility: Annotated[DomainFloat, Field(ge=0, le=1)] | None = None
    vector_similarity: Annotated[DomainFloat, Field(ge=0, le=1)] | None = None
    lexical_similarity: Annotated[DomainFloat, Field(ge=0, le=1)] | None = None
    topology_relation: HistoricalTopologyRelation | None = None
    topology_hops: Annotated[int, Field(strict=True, ge=1)] | None = None
    topology_path_distance_km: Annotated[DomainFloat, Field(ge=0)] | None = None
    topology_proximity: Annotated[DomainFloat, Field(ge=0, le=1)] | None = None
    upstream_preference: bool | None = None

    @model_validator(mode="after")
    def validate_strategy_components(self) -> Self:
        ensure_unique_tuple(self.reasons, field_name="historical match reasons")
        required = {
            HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW: {
                "same_comid",
                "parameter_overlap_ratio",
                "window_overlap_ratio",
                "lexical_similarity",
            },
            HistoricalSearchStrategy.NEARBY_COMID_PARAMETER: {
                "spatial_proximity",
                "parameter_overlap_ratio",
                "lexical_similarity",
            },
            HistoricalSearchStrategy.PARAMETER_SEASON: {
                "same_comid",
                "season_similarity",
                "duration_compatibility",
                "parameter_overlap_ratio",
                "lexical_similarity",
            },
            HistoricalSearchStrategy.SEMANTIC_CASE_QUERY: {
                "vector_similarity",
                "parameter_overlap_ratio",
                "lexical_similarity",
            },
            HistoricalSearchStrategy.TOPOLOGY_NEIGHBORHOOD_PARAMETER: {
                "topology_relation",
                "topology_hops",
                "topology_path_distance_km",
                "topology_proximity",
                "upstream_preference",
                "parameter_overlap_ratio",
                "lexical_similarity",
            },
        }[self.strategy]
        supplied = {
            field_name for field_name in type(self).model_fields
            if field_name not in {"strategy", "reasons"}
            if getattr(self, field_name) is not None
        }
        missing = required - supplied
        forbidden = supplied - required
        if missing:
            raise ValueError(f"strategy requires component {sorted(missing)[0]}")
        if forbidden:
            raise ValueError(f"strategy forbids component {sorted(forbidden)[0]}")
        if self.strategy is HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW and (
            self.same_comid is not True
        ):
            raise ValueError("exact strategy requires component same_comid=true")
        expected_reasons = {
            _COMPONENT_REASON[field_name]
            for field_name in supplied
            if field_name in _COMPONENT_REASON
            and not (field_name == "same_comid" and self.same_comid is False)
        }
        if set(self.reasons) != expected_reasons:
            raise ValueError("historical match reasons must match supplied components")
        return self
