from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.application.analysis_runs.models import AgentRole
from app.domain import (
    AssessmentAvailability,
    CanonicalCaseQueryV1,
    CanonicalIntentToken,
    CanonicalWindowDescriptorV1,
    CoverageStatus,
    EmbeddingDistanceMetric,
    EmbeddingProfile,
    EmbeddingVector,
    EvidenceCoverage,
    EvidenceResultEligibility,
    HistoricalCaseRef,
    HistoricalMatchReason,
    HistoricalResultCellProjection,
    HistoricalResultProjection,
    HistoricalSearchIntentV1,
    HistoricalSearchStrategy,
    HistoricalSupportingFragmentProjection,
    InferenceMode,
    MeteorologicalSeason,
    ModelCapability,
    ModelCatalogSnapshot,
    ModelRole,
    OutcomeStatus,
    ParameterKey,
    RAGCaseProjectionV1,
    RAGCaseWindowV1,
    RAGFragmentProjectionV1,
    RAGIndexGeneration,
    RAGIndexGenerationStatus,
    RAGIndexJob,
    RAGIndexJobStatus,
    RAGQualityTier,
    RAGScoreComponentsV1,
    ResultSource,
    RiskLevel,
    TimeRange,
    TimeResolution,
    TrendDirection,
)


NOW = datetime(2026, 10, 7, 9, tzinfo=timezone.utc)
WINDOW = TimeRange(
    start=datetime(2026, 7, 10, tzinfo=timezone.utc),
    end=datetime(2026, 7, 17, tzinfo=timezone.utc),
)
PARAMETER = ParameterKey(code_system="usgs", code="00300")


def embedding_profile(**updates: object) -> EmbeddingProfile:
    values: dict[str, object] = {
        "provider": "test-provider",
        "model": "embedding-v1",
        "output_dimensions": 8,
        "max_input_tokens": 512,
        "distance_metric": EmbeddingDistanceMetric.COSINE,
        "semantic_min_similarity": 0.78,
        "canonical_query_schema_version": 1,
        "canonical_template_version": 1,
        "projection_schema_version": 1,
    }
    values.update(updates)
    return EmbeddingProfile(**values)


def case_ref(**updates: object) -> HistoricalCaseRef:
    values: dict[str, object] = {
        "generation_id": "generation-1",
        "case_id": "case-1",
        "run_id": "run-1",
        "session_id": "session-1",
        "target_comid": "12345678",
        "analysis_context_item_id": "analysis-context-1",
        "result_item_id": "result-1",
    }
    values.update(updates)
    return HistoricalCaseRef(**values)


def cell() -> HistoricalResultCellProjection:
    return HistoricalResultCellProjection(
        parameter=PARAMETER,
        window_id="historical-window-0001",
        time_range=WINDOW,
        outcome_status=OutcomeStatus.DIRECTIONAL,
        representative_value=None,
        unit_id=None,
        trend=TrendDirection.FALLING,
        risk_level=RiskLevel.MEDIUM,
        confidence=0.72,
        source=ResultSource.PREDICTED,
    )


def canonical_query() -> CanonicalCaseQueryV1:
    return CanonicalCaseQueryV1(
        mode=InferenceMode.PREDICTION,
        requested_parameter_keys=(PARAMETER,),
        window_descriptors=(
            CanonicalWindowDescriptorV1(
                window_kind=InferenceMode.PREDICTION,
                duration_days="7",
                local_months=(7,),
                season_buckets=(MeteorologicalSeason.JJA,),
                resolution=TimeResolution.TWENTY_FOUR_HOURS,
            ),
        ),
        normalized_intent_tokens=(
            CanonicalIntentToken.ANALYZE_CURRENT_STATE,
            CanonicalIntentToken.PREDICT,
            CanonicalIntentToken.REQUIRES_DISCHARGE_SUPPORT,
        ),
    )


@pytest.mark.parametrize("strategy", tuple(HistoricalSearchStrategy))
def test_search_intent_accepts_each_closed_strategy_without_hidden_scope_fields(
    strategy: HistoricalSearchStrategy,
) -> None:
    intent = HistoricalSearchIntentV1(
        strategy=strategy,
        parameter_keys=(PARAMETER,),
        window_ids=("w000",),
        limit=5,
    )

    assert set(intent.model_dump(mode="json")) == {
        "strategy",
        "parameter_keys",
        "window_ids",
        "limit",
    }
    assert HistoricalSearchIntentV1.model_validate_json(intent.model_dump_json()) == intent
    assert HistoricalSearchIntentV1(
        strategy=strategy,
        parameter_keys=(PARAMETER,),
        window_ids=("w000",),
    ).limit == 5
    with pytest.raises(ValidationError, match="extra_forbidden"):
        HistoricalSearchIntentV1.model_validate(
            {**intent.model_dump(mode="python"), "query": "raw text"}
        )


@pytest.mark.parametrize(
    "update",
    (
        {"parameter_keys": ()},
        {"parameter_keys": (PARAMETER, PARAMETER)},
        {"window_ids": ()},
        {"window_ids": ("w000", "w000")},
        {"limit": 0},
        {"limit": 6},
    ),
)
def test_search_intent_rejects_empty_duplicate_or_out_of_bounds_fields(
    update: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "strategy": HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW,
        "parameter_keys": (PARAMETER,),
        "window_ids": ("w000",),
        "limit": 1,
    }
    values.update(update)

    with pytest.raises(ValidationError):
        HistoricalSearchIntentV1(**values)


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("query", "free text"),
        ("source_run_id", "run-old"),
        ("current_comid", "12345678"),
        ("start_date", "2026-07-10"),
        ("radius_km", 25.0),
        ("max_hops", 3),
        ("profile_id", "sha256:agent-selected"),
    ),
)
def test_search_intent_rejects_every_agent_controlled_scope_field(
    field_name: str,
    value: object,
) -> None:
    body = {
        "strategy": HistoricalSearchStrategy.SEMANTIC_CASE_QUERY,
        "parameter_keys": (PARAMETER,),
        "window_ids": ("w000",),
        field_name: value,
    }

    with pytest.raises(ValidationError, match="extra_forbidden"):
        HistoricalSearchIntentV1.model_validate(body)


def test_case_reference_requires_full_multi_waterbody_identity() -> None:
    first = case_ref()
    second = case_ref(session_id="session-2", result_item_id="result-2")

    assert first != second
    assert first.run_id == second.run_id
    for field_name in (
        "generation_id",
        "case_id",
        "run_id",
        "session_id",
        "target_comid",
        "analysis_context_item_id",
        "result_item_id",
    ):
        values = first.model_dump(mode="python")
        values.pop(field_name)
        with pytest.raises(ValidationError):
            HistoricalCaseRef.model_validate(values)


@pytest.mark.parametrize(
    ("strategy", "component", "reason"),
    (
        (
            HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW,
            {
                "same_comid": True,
                "parameter_overlap_ratio": 1.0,
                "window_overlap_ratio": 1.0,
                "lexical_similarity": 0.5,
            },
            (
                HistoricalMatchReason.SAME_COMID,
                HistoricalMatchReason.PARAMETER_OVERLAP,
                HistoricalMatchReason.WINDOW_OVERLAP,
                HistoricalMatchReason.LEXICAL_SIMILARITY,
            ),
        ),
        (
            HistoricalSearchStrategy.NEARBY_COMID_PARAMETER,
            {
                "spatial_proximity": 0.8,
                "parameter_overlap_ratio": 1.0,
                "lexical_similarity": 0.5,
            },
            (
                HistoricalMatchReason.NEARBY_COMID,
                HistoricalMatchReason.PARAMETER_OVERLAP,
                HistoricalMatchReason.LEXICAL_SIMILARITY,
            ),
        ),
        (
            HistoricalSearchStrategy.PARAMETER_SEASON,
            {
                "same_comid": False,
                "season_similarity": 1.0,
                "duration_compatibility": 0.8,
                "parameter_overlap_ratio": 1.0,
                "lexical_similarity": 0.5,
            },
            (
                HistoricalMatchReason.SEASON_MATCH,
                HistoricalMatchReason.PARAMETER_OVERLAP,
                HistoricalMatchReason.LEXICAL_SIMILARITY,
            ),
        ),
        (
            HistoricalSearchStrategy.SEMANTIC_CASE_QUERY,
            {
                "vector_similarity": 0.9,
                "parameter_overlap_ratio": 1.0,
                "lexical_similarity": 0.5,
            },
            (
                HistoricalMatchReason.VECTOR_SIMILARITY,
                HistoricalMatchReason.PARAMETER_OVERLAP,
                HistoricalMatchReason.LEXICAL_SIMILARITY,
            ),
        ),
        (
            HistoricalSearchStrategy.TOPOLOGY_NEIGHBORHOOD_PARAMETER,
            {
                "topology_relation": "ancestor",
                "topology_hops": 1,
                "topology_path_distance_km": 2.5,
                "topology_proximity": 0.75,
                "upstream_preference": True,
                "parameter_overlap_ratio": 1.0,
                "lexical_similarity": 0.5,
            },
            (
                HistoricalMatchReason.TOPOLOGY_PROXIMITY,
                HistoricalMatchReason.PARAMETER_OVERLAP,
                HistoricalMatchReason.LEXICAL_SIMILARITY,
            ),
        ),
    ),
)
def test_score_components_are_typed_per_strategy(
    strategy: HistoricalSearchStrategy,
    component: dict[str, object],
    reason: tuple[HistoricalMatchReason, ...],
) -> None:
    score = RAGScoreComponentsV1(
        strategy=strategy,
        reasons=reason,
        **component,
    )

    assert score.strategy is strategy
    with pytest.raises(ValidationError, match="component"):
        RAGScoreComponentsV1(
            strategy=strategy,
            reasons=reason,
            **{key: value for key, value in component.items() if key != next(iter(component))},
        )


def test_result_projection_and_supporting_fragments_are_bounded_and_typed() -> None:
    projection = HistoricalResultProjection(
        mode=InferenceMode.PREDICTION,
        source_completed_at=NOW,
        overall_risk=RiskLevel.MEDIUM,
        overall_confidence=0.8,
        availability=AssessmentAvailability.PARTIAL,
        cells=(cell(),),
    )
    fragment = HistoricalSupportingFragmentProjection(
        original_item_id="fragment-item-1",
        evidence_id="evidence-1",
        parameter=PARAMETER,
        valid_time_range=WINDOW,
        quality_class="valid",
        applicability="contextual_only",
    )

    assert projection.cells == (cell(),)
    assert fragment.applicability.value == "contextual_only"
    with pytest.raises(ValidationError, match="at most 2"):
        from app.domain import HistoricalTranscriptRecord

        HistoricalTranscriptRecord.model_validate(
            {
                "supporting_fragments": (fragment, fragment, fragment),
            }
        )


def test_index_projection_contracts_keep_source_schema_and_safe_typed_fields() -> None:
    result = HistoricalResultProjection(
        mode=InferenceMode.PREDICTION,
        source_completed_at=NOW,
        overall_risk=RiskLevel.MEDIUM,
        overall_confidence=0.8,
        availability=AssessmentAvailability.PARTIAL,
        cells=(cell(),),
    )
    case = RAGCaseProjectionV1(
        case_ref=case_ref(),
        profile_id=embedding_profile().profile_id,
        mode=InferenceMode.PREDICTION,
        parameter_keys=(PARAMETER,),
        window_ids=(cell().window_id,),
        windows=(
            RAGCaseWindowV1(
                window_id=cell().window_id,
                time_range=WINDOW,
                window_kind=InferenceMode.PREDICTION,
                local_months=(7,),
                season_buckets=(MeteorologicalSeason.JJA,),
                season_year=2026,
                duration_seconds=int((WINDOW.end - WINDOW.start).total_seconds()),
                resolution=TimeResolution.TWENTY_FOUR_HOURS,
                parameter_keys=(PARAMETER,),
            ),
        ),
        source_completed_at=NOW,
        canonical_query=canonical_query().canonical_text,
        canonical_query_digest=canonical_query().canonical_digest,
        result_projection=result,
        embedding=EmbeddingVector(values=(0.0,) * 8),
    )
    fragment = RAGFragmentProjectionV1(
        case_ref=case.case_ref,
        source_fragment_item_id="fragment-item-1",
        evidence_id="evidence-1",
        parameter=PARAMETER,
        window_id=cell().window_id,
        valid_time_range=WINDOW,
        quality_tier=RAGQualityTier.VALID,
        coverage=EvidenceCoverage(
            status=CoverageStatus.PARTIAL,
            observed_count=1,
            expected_count=None,
            ratio=None,
            covered_duration_seconds=0,
            window_duration_seconds=int((WINDOW.end - WINDOW.start).total_seconds()),
            largest_gap_seconds=None,
        ),
        applicability="direct_measurement",
        result_eligibility=EvidenceResultEligibility.OBSERVED_OR_INFERRED,
        provenance_identity="sha256:provenance",
    )

    assert case.source_content_type == "inference_result"
    assert case.source_content_schema_version == 1
    assert case.projection_schema_version == 1
    assert fragment.source_content_type == "evidence_fragment"
    assert fragment.source_content_schema_version == 1
    assert fragment.projection_schema_version == 1


def test_embedding_profile_identity_is_stable_and_excludes_secrets() -> None:
    first = embedding_profile()
    second = embedding_profile()

    assert first == second
    assert first.profile_id == first.profile_digest
    assert first.profile_id.startswith("sha256:")
    assert "secret" not in first.model_dump_json().casefold()
    with pytest.raises(ValidationError, match="extra_forbidden"):
        EmbeddingProfile.model_validate(
            {**first.model_dump(mode="python"), "api_key": "must-not-be-stored"}
        )
    with pytest.raises(ValidationError, match="digest"):
        EmbeddingProfile.model_validate(
            {**first.model_dump(mode="python"), "profile_digest": "sha256:wrong"}
        )


@pytest.mark.parametrize(
    "update",
    (
        {"output_dimensions": 0},
        {"max_input_tokens": 0},
        {"distance_metric": "euclidean"},
        {"semantic_min_similarity": -0.01},
        {"semantic_min_similarity": 1.01},
        {"canonical_query_schema_version": 2},
        {"canonical_template_version": 2},
        {"projection_schema_version": 2},
    ),
)
def test_embedding_profile_rejects_invalid_dimensions_metric_threshold_or_version(
    update: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        embedding_profile(**update)


def test_catalog_requires_exactly_one_exclusive_embedding_capability() -> None:
    profile = embedding_profile()
    embedding = ModelCapability(
        provider=profile.provider,
        model=profile.model,
        roles=(ModelRole.EMBEDDING,),
        embedding_profile=profile,
    )
    parser = ModelCapability(
        provider="test-provider",
        model="parser-v1",
        roles=(ModelRole.QUERY_PARSER,),
    )

    assert ModelCatalogSnapshot(capabilities=(parser, embedding)).embedding_profile == profile
    with pytest.raises(ValidationError, match="exactly one embedding"):
        ModelCatalogSnapshot(capabilities=(parser,))
    with pytest.raises(ValidationError, match="exactly one embedding"):
        other_profile = embedding_profile(model="embedding-v2")
        ModelCatalogSnapshot(
            capabilities=(
                embedding,
                ModelCapability(
                    provider=other_profile.provider,
                    model=other_profile.model,
                    roles=(ModelRole.EMBEDDING,),
                    embedding_profile=other_profile,
                ),
            )
        )
    with pytest.raises(ValidationError, match="exclusive"):
        ModelCapability(
            provider=profile.provider,
            model=profile.model,
            roles=(ModelRole.EMBEDDING, ModelRole.WATERBODY),
            context_window_tokens=100,
            max_output_tokens=10,
            embedding_profile=profile,
        )
    assert "embedding" not in {role.value for role in AgentRole}


def test_generation_and_job_statuses_are_closed_and_state_fields_are_consistent() -> None:
    profile = embedding_profile()
    generation = RAGIndexGeneration(
        generation_id="generation-1",
        profile=profile,
        status=RAGIndexGenerationStatus.BUILDING,
        source_cutoff_created_at=NOW,
        source_cutoff_item_id="result-cutoff",
        created_at=NOW,
    )
    job = RAGIndexJob(
        job_id="job-1",
        generation_id=generation.generation_id,
        source_run_id="run-1",
        source_session_id="session-1",
        source_result_item_id="result-1",
        projection_schema_version=1,
        status=RAGIndexJobStatus.PENDING,
        attempts=0,
        created_at=NOW,
        updated_at=NOW,
    )

    assert tuple(status.value for status in RAGIndexGenerationStatus) == (
        "building",
        "active",
        "retired",
        "failed",
    )
    assert tuple(status.value for status in RAGIndexJobStatus) == (
        "pending",
        "leased",
        "retry_wait",
        "completed",
        "excluded",
        "failed",
    )
    assert generation.profile.profile_id == profile.profile_id
    assert job.status is RAGIndexJobStatus.PENDING
    with pytest.raises(ValidationError, match="lease"):
        job.model_copy(
            update={"status": RAGIndexJobStatus.LEASED},
        ).__class__.model_validate(
            job.model_copy(update={"status": RAGIndexJobStatus.LEASED}).model_dump()
        )
