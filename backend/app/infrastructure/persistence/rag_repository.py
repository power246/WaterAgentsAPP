import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import json
from uuid import UUID, uuid4

from psycopg.types.range import Range
from sqlalchemy import (
    Float,
    and_,
    case,
    cast,
    delete,
    exists,
    func,
    insert,
    literal,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.domain import (
    AuthorizedRAGSearchQuery,
    CanonicalCaseQueryV1,
    EmbeddingProfile,
    HistoricalCaseRef,
    HistoricalResultProjection,
    HistoricalSearchStrategy,
    InferenceMode,
    ParameterKey,
    PreparedRAGCaseProjectionV1,
    RAGCandidateRow,
    RAGCaseWindowV1,
    RAGCleanupDependency,
    RAGCleanupPlan,
    RAGCleanupResult,
    RAGCaseProjectionV1,
    RAGFragmentProjectionV1,
    RAGIndexGeneration,
    RAGIndexGenerationStatus,
    RAGIndexJob,
    RAGIndexJobStatus,
    RAGJobCounts,
    RAGQualityTier,
    RAGSourceCutoff,
    TimeRange,
    TimeResolution,
)
from app.application.transcript_memory.search_policies import (
    rank_projection,
)
from app.infrastructure.persistence.records import RAGConsumptionLinkRecord
from app.infrastructure.persistence.rag_consumption import has_valid_consumption_source
from app.infrastructure.persistence.schema import (
    analysis_run_idempotency_tombstones,
    analysis_runs,
    analysis_sessions,
    rag_case_projections,
    rag_case_windows,
    rag_consumption_links,
    rag_fragment_projections,
    rag_index_generations,
    rag_index_jobs,
    transcript_contents,
    transcript_items,
)
from app.ports.errors import PortUnavailableError


class RAGPersistenceConflictError(RuntimeError):
    """A persisted RAG identity or state transition conflicts with the request."""


def _uuid(value: str | UUID, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be a UUID") from error


def _parameter_key_text(parameter) -> str:
    return json.dumps(
        parameter.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _generation_from_row(row) -> RAGIndexGeneration:
    profile = EmbeddingProfile(
        profile_id=row["profile_id"],
        provider=row["provider"],
        model=row["model"],
        output_dimensions=row["output_dimensions"],
        max_input_tokens=row["max_input_tokens"],
        distance_metric=row["distance_metric"],
        semantic_min_similarity=row["semantic_min_similarity"],
        canonical_query_schema_version=row["canonical_query_schema_version"],
        canonical_template_version=row["canonical_template_version"],
        projection_schema_version=row["projection_schema_version"],
        profile_digest=row["profile_digest"],
    )
    return RAGIndexGeneration(
        generation_id=str(row["generation_id"]),
        profile=profile,
        status=row["status"],
        source_cutoff_created_at=row["source_cutoff_created_at"],
        source_cutoff_item_id=str(row["source_cutoff_item_id"]),
        created_at=row["created_at"],
        activated_at=row["activated_at"],
        retired_at=row["retired_at"],
        failed_at=row["failed_at"],
        last_safe_error_code=row["last_safe_error_code"],
    )


def _job_from_row(row) -> RAGIndexJob:
    return RAGIndexJob(
        job_id=str(row["job_id"]),
        generation_id=str(row["generation_id"]),
        source_run_id=str(row["source_run_id"]),
        source_session_id=str(row["source_session_id"]),
        source_result_item_id=str(row["source_result_item_id"]),
        projection_schema_version=row["projection_schema_version"],
        status=row["status"],
        attempts=row["attempts"],
        next_attempt_at=row["next_attempt_at"],
        lease_owner=row["lease_owner"],
        lease_until=row["lease_until"],
        last_safe_error_code=row["last_safe_error_code"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
    )


def _prepared_projection_from_rows(
    row,
    window_rows: tuple,
) -> PreparedRAGCaseProjectionV1:
    case_ref = HistoricalCaseRef(
        generation_id=str(row["generation_id"]),
        case_id=str(row["case_id"]),
        run_id=str(row["source_run_id"]),
        session_id=str(row["source_session_id"]),
        target_comid=row["target_comid"],
        analysis_context_item_id=str(row["analysis_context_item_id"]),
        result_item_id=str(row["source_result_item_id"]),
    )
    mode = InferenceMode(row["mode"])
    canonical = CanonicalCaseQueryV1.model_validate_json(row["canonical_query"])
    by_window_id = {item["window_id"]: item for item in window_rows}
    windows = tuple(
        RAGCaseWindowV1(
            window_id=window_id,
            time_range=TimeRange(
                start=by_window_id[window_id]["time_range"].lower,
                end=by_window_id[window_id]["time_range"].upper,
            ),
            window_kind=(
                mode
                if mode is not InferenceMode.MIXED
                else _matching_window_kind(canonical, by_window_id[window_id])
            ),
            local_months=tuple(by_window_id[window_id]["local_months"]),
            season_buckets=(by_window_id[window_id]["season_bucket"],),
            season_year=by_window_id[window_id]["season_year"],
            duration_seconds=by_window_id[window_id]["duration_seconds"],
            resolution=TimeResolution(by_window_id[window_id]["resolution"]),
            parameter_keys=tuple(
                ParameterKey.model_validate_json(item)
                for item in by_window_id[window_id]["parameter_keys"]
            ),
        )
        for window_id in row["window_ids"]
    )
    values = dict(
        case_ref=case_ref,
        profile_id=row["profile_id"],
        projection_schema_version=row["projection_schema_version"],
        source_content_type=row["source_content_type"],
        source_content_schema_version=row["source_content_schema_version"],
        mode=mode,
        parameter_keys=tuple(
            ParameterKey.model_validate_json(item) for item in row["parameter_keys"]
        ),
        window_ids=tuple(row["window_ids"]),
        windows=windows,
        source_completed_at=row["source_completed_at"],
        canonical_query=row["canonical_query"],
        canonical_query_digest=row["canonical_query_digest"],
        result_projection=HistoricalResultProjection.model_validate(
            row["result_projection"]
        ),
        projection_digest=row["projection_digest"],
    )
    if mode is InferenceMode.MIXED:
        # The V1 normalized table stores case mode plus descriptor facts, not a
        # per-row mode. Hydration rebuilds and authenticates the exact source
        # projection; ranking only needs the persisted structured signals.
        return PreparedRAGCaseProjectionV1.model_construct(**values)
    return PreparedRAGCaseProjectionV1(**values)


def _matching_window_kind(canonical, row) -> InferenceMode:
    seasons = (row["season_bucket"],)
    matches = tuple(
        descriptor.window_kind
        for descriptor in canonical.window_descriptors
        if descriptor.local_months == tuple(row["local_months"])
        and tuple(season.value for season in descriptor.season_buckets) == seasons
        and descriptor.resolution.value == row["resolution"]
        and round(float(descriptor.duration_days) * 86_400)
        == row["duration_seconds"]
    )
    if not matches:
        raise ValueError("persisted RAG window has no canonical descriptor")
    return matches[0]


def _topology_preliminary_score(query, scope) -> float:
    assert scope.topology_hops is not None
    assert scope.topology_path_distance_km is not None
    assert scope.topology_relation is not None
    assert query.topology_max_hops is not None
    assert query.topology_max_distance_km is not None
    hop = 1 - min(
        (scope.topology_hops - 1) / max(query.topology_max_hops - 1, 1),
        1,
    )
    distance = 1 - min(
        scope.topology_path_distance_km / query.topology_max_distance_km,
        1,
    )
    topology = 0.6 * hop + 0.4 * distance
    upstream = scope.topology_relation.value == "ancestor"
    return 0.55 * topology + 0.25 * float(upstream)


class PostgresRAGRepository:
    """Transaction owner for M8 generation, job, projection, and ownership rows."""

    def __init__(self, engine: AsyncEngine, *, operation_timeout_seconds: float) -> None:
        if operation_timeout_seconds <= 0:
            raise ValueError("operation_timeout_seconds must be positive")
        self._engine = engine
        self._operation_timeout_seconds = operation_timeout_seconds

    async def create_building_generation(
        self,
        profile: EmbeddingProfile,
        source_cutoff_created_at: datetime,
        source_cutoff_item_id: str,
    ) -> RAGIndexGeneration:
        now = datetime.now(UTC)
        generation = RAGIndexGeneration(
            generation_id=str(uuid4()),
            profile=profile,
            status=RAGIndexGenerationStatus.BUILDING,
            source_cutoff_created_at=source_cutoff_created_at,
            source_cutoff_item_id=source_cutoff_item_id,
            created_at=now,
        )
        values = self._generation_values(generation)
        try:
            async with self._transaction() as connection:
                await connection.execute(insert(rag_index_generations).values(**values))
        except IntegrityError as error:
            raise RAGPersistenceConflictError(
                "a building generation already exists or its identity is invalid"
            ) from error
        return generation

    async def read_generation(self, generation_id: str) -> RAGIndexGeneration | None:
        row = await self._read_one(
            select(rag_index_generations).where(
                rag_index_generations.c.generation_id
                == _uuid(generation_id, "generation_id")
            )
        )
        return None if row is None else _generation_from_row(row)

    async def list_generations(self) -> tuple[RAGIndexGeneration, ...]:
        rows = await self._read_all(
            select(rag_index_generations).order_by(
                rag_index_generations.c.created_at,
                rag_index_generations.c.generation_id,
            )
        )
        return tuple(_generation_from_row(row) for row in rows)

    async def read_active_generation(self) -> RAGIndexGeneration | None:
        row = await self._read_one(
            select(rag_index_generations).where(
                rag_index_generations.c.status
                == RAGIndexGenerationStatus.ACTIVE.value
            )
        )
        return None if row is None else _generation_from_row(row)

    async def active_projection_dimensions(self) -> tuple[int, ...]:
        rows = await self._read_all(
            select(rag_case_projections.c.embedding_dimensions)
            .join(
                rag_index_generations,
                rag_index_generations.c.generation_id
                == rag_case_projections.c.generation_id,
            )
            .where(
                rag_index_generations.c.status
                == RAGIndexGenerationStatus.ACTIVE.value
            )
            .distinct()
            .order_by(rag_case_projections.c.embedding_dimensions)
        )
        return tuple(row["embedding_dimensions"] for row in rows)

    async def oldest_incomplete_job_at(
        self,
        generation_id: str,
    ) -> datetime | None:
        identifier = _uuid(generation_id, "generation_id")
        row = await self._read_one(
            select(func.min(rag_index_jobs.c.created_at).label("created_at")).where(
                rag_index_jobs.c.generation_id == identifier,
                rag_index_jobs.c.status.in_(
                    (
                        RAGIndexJobStatus.PENDING.value,
                        RAGIndexJobStatus.LEASED.value,
                        RAGIndexJobStatus.RETRY_WAIT.value,
                        RAGIndexJobStatus.FAILED.value,
                    )
                ),
            )
        )
        return None if row is None else row["created_at"]

    async def search_candidates(
        self,
        query: AuthorizedRAGSearchQuery,
    ) -> tuple[RAGCandidateRow, ...]:
        """Apply generation/scope/lifecycle hard filters before exact ranking."""

        generation_id = _uuid(query.generation_id, "generation_id")
        requested_parameters = sorted(
            {_parameter_key_text(item) for item in query.intent.parameter_keys}
        )
        source_item = transcript_items.alias("rag_search_source_item")
        context_item = transcript_items.alias("rag_search_context_item")
        source = (
            rag_case_projections.join(
                rag_index_jobs,
                and_(
                    rag_index_jobs.c.generation_id
                    == rag_case_projections.c.generation_id,
                    rag_index_jobs.c.source_run_id
                    == rag_case_projections.c.source_run_id,
                    rag_index_jobs.c.source_session_id
                    == rag_case_projections.c.source_session_id,
                    rag_index_jobs.c.source_result_item_id
                    == rag_case_projections.c.source_result_item_id,
                ),
            )
            .join(
                source_item,
                and_(
                    source_item.c.run_id == rag_case_projections.c.source_run_id,
                    source_item.c.session_id
                    == rag_case_projections.c.source_session_id,
                    source_item.c.item_id
                    == rag_case_projections.c.source_result_item_id,
                ),
            )
            .join(
                context_item,
                and_(
                    context_item.c.run_id == rag_case_projections.c.source_run_id,
                    context_item.c.session_id.is_not(None),
                    context_item.c.item_id
                    == rag_case_projections.c.analysis_context_item_id,
                ),
            )
            .join(
                analysis_runs,
                analysis_runs.c.run_id == rag_case_projections.c.source_run_id,
            )
            .join(
                analysis_sessions,
                and_(
                    analysis_sessions.c.run_id
                    == rag_case_projections.c.source_run_id,
                    analysis_sessions.c.session_id
                    == rag_case_projections.c.source_session_id,
                ),
            )
        )
        predicates = [
            rag_case_projections.c.generation_id == generation_id,
            rag_case_projections.c.profile_id == query.profile_id,
            rag_case_projections.c.projection_schema_version == 1,
            rag_case_projections.c.source_content_type == "inference_result",
            rag_case_projections.c.source_content_schema_version == 1,
            rag_case_projections.c.target_comid.in_(query.candidate_comids),
            rag_case_projections.c.parameter_keys.op("&&")(requested_parameters),
            rag_index_jobs.c.status == RAGIndexJobStatus.COMPLETED.value,
            analysis_runs.c.status == "completed",
            analysis_sessions.c.status == "completed",
            source_item.c.content_type == "inference_result",
            source_item.c.content_schema_version == 1,
            source_item.c.rag_eligible.is_(True),
            context_item.c.content_type == "query_understood",
            context_item.c.content_schema_version == 1,
        ]
        strategy = query.intent.strategy
        if strategy in {
            HistoricalSearchStrategy.PARAMETER_SEASON,
            HistoricalSearchStrategy.SEMANTIC_CASE_QUERY,
        }:
            predicates.append(
                rag_case_projections.c.mode
                == CanonicalCaseQueryV1.model_validate_json(
                    query.canonical_query
                ).mode.value
            )
        if strategy is HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW:
            overlaps = or_(
                *(
                    rag_case_windows.c.time_range.op("&&")(
                        Range(
                            window.time_range.start,
                            window.time_range.end,
                            "[)",
                        )
                    )
                    for window in query.query_windows
                )
            )
            predicates.append(
                exists(
                    select(literal(1)).where(
                        rag_case_windows.c.generation_id
                        == rag_case_projections.c.generation_id,
                        rag_case_windows.c.case_id
                        == rag_case_projections.c.case_id,
                        rag_case_windows.c.parameter_keys.op("&&")(
                            requested_parameters
                        ),
                        overlaps,
                    )
                )
            )
        elif strategy is HistoricalSearchStrategy.PARAMETER_SEASON:
            seasons = sorted(
                {
                    season.value
                    for window in query.query_windows
                    for season in window.season_buckets
                }
            )
            predicates.append(
                exists(
                    select(literal(1)).where(
                        rag_case_windows.c.generation_id
                        == rag_case_projections.c.generation_id,
                        rag_case_windows.c.case_id
                        == rag_case_projections.c.case_id,
                        rag_case_windows.c.parameter_keys.op("&&")(
                            requested_parameters
                        ),
                        rag_case_windows.c.season_bucket.in_(seasons),
                    )
                )
            )

        raw_lexical = func.ts_rank_cd(
            rag_case_projections.c.search_document,
            func.plainto_tsquery("simple", query.canonical_query),
        )
        lexical = raw_lexical / (1.0 + raw_lexical)
        vector_similarity = literal(None)
        preliminary = lexical
        if strategy is HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW:
            overlap_seconds = sum(
                (
                    select(
                        func.coalesce(
                            func.sum(
                                func.extract(
                                    "epoch",
                                    func.least(
                                        func.upper(rag_case_windows.c.time_range),
                                        window.time_range.end,
                                    )
                                    - func.greatest(
                                        func.lower(rag_case_windows.c.time_range),
                                        window.time_range.start,
                                    ),
                                )
                            ),
                            0.0,
                        )
                    )
                    .where(
                        rag_case_windows.c.generation_id
                        == rag_case_projections.c.generation_id,
                        rag_case_windows.c.case_id
                        == rag_case_projections.c.case_id,
                        rag_case_windows.c.time_range.op("&&")(
                            Range(
                                window.time_range.start,
                                window.time_range.end,
                                "[)",
                            )
                        ),
                    )
                    .correlate(rag_case_projections)
                    .scalar_subquery()
                    for window in query.query_windows
                )
            )
            requested_seconds = sum(
                (window.time_range.end - window.time_range.start).total_seconds()
                for window in query.query_windows
            )
            preliminary = (
                0.75 * overlap_seconds / requested_seconds + 0.25 * lexical
            )
        elif strategy is HistoricalSearchStrategy.SEMANTIC_CASE_QUERY:
            assert query.query_embedding is not None
            distance = cast(
                rag_case_projections.c.embedding.op("OPERATOR(public.<=>)")(
                    list(query.query_embedding.values)
                ),
                Float,
            )
            vector_similarity = func.greatest(
                0.0,
                func.least(1.0, 1.0 - distance / 2.0),
            )
            assert query.semantic_min_similarity is not None
            threshold = query.semantic_min_similarity
            predicates.append(vector_similarity >= threshold)
            preliminary = 0.85 * vector_similarity + 0.15 * lexical
        elif strategy is HistoricalSearchStrategy.NEARBY_COMID_PARAMETER:
            assert query.spatial_radius_km is not None
            distance_by_comid = case(
                *(
                    (
                        rag_case_projections.c.target_comid == item.comid,
                        item.spatial_distance_km,
                    )
                    for item in query.comid_scope
                ),
                else_=query.spatial_radius_km,
            )
            preliminary = (
                0.70 * (1.0 - distance_by_comid / query.spatial_radius_km)
                + 0.30 * lexical
            )
        elif strategy is HistoricalSearchStrategy.PARAMETER_SEASON:
            requested_seasons = {
                season.value
                for window in query.query_windows
                for season in window.season_buckets
            }
            shared_seasons = (
                select(func.count(func.distinct(rag_case_windows.c.season_bucket)))
                .where(
                    rag_case_windows.c.generation_id
                    == rag_case_projections.c.generation_id,
                    rag_case_windows.c.case_id == rag_case_projections.c.case_id,
                    rag_case_windows.c.season_bucket.in_(requested_seasons),
                )
                .correlate(rag_case_projections)
                .scalar_subquery()
            )
            duration_pairs = tuple(
                (
                    select(
                        func.coalesce(
                            func.max(
                                cast(
                                    func.least(
                                        rag_case_windows.c.duration_seconds,
                                        window.duration_seconds,
                                    ),
                                    Float,
                                )
                                / func.greatest(
                                    rag_case_windows.c.duration_seconds,
                                    window.duration_seconds,
                                )
                            ),
                            0.0,
                        )
                    )
                    .where(
                        rag_case_windows.c.generation_id
                        == rag_case_projections.c.generation_id,
                        rag_case_windows.c.case_id
                        == rag_case_projections.c.case_id,
                        rag_case_windows.c.season_bucket.in_(
                            tuple(season.value for season in window.season_buckets)
                        ),
                    )
                    .correlate(rag_case_projections)
                    .scalar_subquery()
                )
                for window in query.query_windows
            )
            best_duration = (
                duration_pairs[0]
                if len(duration_pairs) == 1
                else func.greatest(*duration_pairs)
            )
            preliminary = (
                case(
                    (
                        rag_case_projections.c.target_comid == query.current_comid,
                        0.35,
                    ),
                    else_=0.0,
                )
                + 0.35 * shared_seasons / len(requested_seasons)
                + 0.20 * best_duration
                + 0.10 * lexical
            )
        elif strategy is HistoricalSearchStrategy.TOPOLOGY_NEIGHBORHOOD_PARAMETER:
            preliminary_by_comid = {
                item.comid: _topology_preliminary_score(query, item)
                for item in query.comid_scope
            }
            preliminary = case(
                *(
                    (
                        rag_case_projections.c.target_comid == comid,
                        score,
                    )
                    for comid, score in preliminary_by_comid.items()
                ),
                else_=0.0,
            ) + 0.20 * lexical

        matched_parameter_count = sum(
            case(
                (
                    rag_case_projections.c.parameter_keys.op("@>")([parameter]),
                    1,
                ),
                else_=0,
            )
            for parameter in requested_parameters
        )
        suspicious_quality = exists(
            select(literal(1)).where(
                rag_fragment_projections.c.generation_id
                == rag_case_projections.c.generation_id,
                rag_fragment_projections.c.case_id
                == rag_case_projections.c.case_id,
                rag_fragment_projections.c.quality_tier
                == RAGQualityTier.SUSPICIOUS.value,
            )
        )
        original_confidence = rag_case_projections.c.result_projection[
            "overall_confidence"
        ].as_float()
        statement = (
            select(
                rag_case_projections,
                lexical.label("lexical_similarity"),
                vector_similarity.label("vector_similarity"),
            )
            .select_from(source)
            .where(*predicates)
            .order_by(
                preliminary.desc(),
                matched_parameter_count.desc(),
                case((suspicious_quality, 1), else_=0),
                original_confidence.desc().nulls_last(),
                rag_case_projections.c.source_completed_at.desc(),
                rag_case_projections.c.source_run_id,
                rag_case_projections.c.source_session_id,
                rag_case_projections.c.source_result_item_id,
            )
            .limit(50)
        )
        rows = await self._read_all(statement)
        if not rows:
            return ()
        identities = tuple(
            (row["generation_id"], row["case_id"]) for row in rows
        )
        window_rows = await self._read_all(
            select(rag_case_windows)
            .where(
                tuple_(
                    rag_case_windows.c.generation_id,
                    rag_case_windows.c.case_id,
                ).in_(identities)
            )
            .order_by(
                rag_case_windows.c.generation_id,
                rag_case_windows.c.case_id,
                rag_case_windows.c.time_range,
                rag_case_windows.c.window_id,
            )
        )
        fragment_rows = await self._read_all(
            select(
                rag_fragment_projections.c.generation_id,
                rag_fragment_projections.c.case_id,
                rag_fragment_projections.c.quality_tier,
            ).where(
                tuple_(
                    rag_fragment_projections.c.generation_id,
                    rag_fragment_projections.c.case_id,
                ).in_(identities)
            )
        )
        windows_by_case: dict[tuple[UUID, UUID], list] = {}
        for row in window_rows:
            windows_by_case.setdefault(
                (row["generation_id"], row["case_id"]), []
            ).append(row)
        suspicious = {
            (row["generation_id"], row["case_id"])
            for row in fragment_rows
            if row["quality_tier"] == RAGQualityTier.SUSPICIOUS.value
        }
        candidates: list[RAGCandidateRow] = []
        for row in rows:
            identity = (row["generation_id"], row["case_id"])
            projection = _prepared_projection_from_rows(
                row,
                tuple(windows_by_case.get(identity, ())),
            )
            candidate = rank_projection(
                query,
                projection,
                lexical_similarity=float(row["lexical_similarity"] or 0),
                vector_similarity=(
                    None
                    if row["vector_similarity"] is None
                    else float(row["vector_similarity"])
                ),
                quality_tier=(
                    RAGQualityTier.SUSPICIOUS
                    if identity in suspicious
                    else RAGQualityTier.VALID
                ),
                original_confidence=(
                    HistoricalResultProjection.model_validate(
                        row["result_projection"]
                    ).overall_confidence
                ),
            )
            candidates.append(candidate)
        return tuple(candidates)

    async def activate_generation(
        self,
        generation_id: str,
        activated_at: datetime,
    ) -> RAGIndexGeneration:
        identifier = _uuid(generation_id, "generation_id")
        try:
            async with self._transaction() as connection:
                row = await self._locked_generation(connection, identifier)
                if row is None or row["status"] != RAGIndexGenerationStatus.BUILDING.value:
                    raise RAGPersistenceConflictError(
                        "only an existing building generation can be activated"
                    )
                await self._validate_generation_ready_for_activation(
                    connection,
                    row,
                )
                await connection.execute(
                    update(rag_index_generations)
                    .where(
                        rag_index_generations.c.status
                        == RAGIndexGenerationStatus.ACTIVE.value
                    )
                    .values(
                        status=RAGIndexGenerationStatus.RETIRED.value,
                        retired_at=activated_at,
                    )
                )
                await connection.execute(
                    update(rag_index_generations)
                    .where(rag_index_generations.c.generation_id == identifier)
                    .values(
                        status=RAGIndexGenerationStatus.ACTIVE.value,
                        activated_at=activated_at,
                    )
                )
                activated = await self._locked_generation(connection, identifier)
        except IntegrityError as error:
            raise RAGPersistenceConflictError(
                "generation activation conflicts with the active generation"
            ) from error
        assert activated is not None
        return _generation_from_row(activated)

    async def retire_generation(
        self,
        generation_id: str,
        retired_at: datetime,
    ) -> RAGIndexGeneration:
        identifier = _uuid(generation_id, "generation_id")
        async with self._transaction() as connection:
            row = await self._locked_generation(connection, identifier)
            if row is None or row["status"] != RAGIndexGenerationStatus.ACTIVE.value:
                raise RAGPersistenceConflictError(
                    "only an existing active generation can be retired"
                )
            await connection.execute(
                update(rag_index_generations)
                .where(rag_index_generations.c.generation_id == identifier)
                .values(
                    status=RAGIndexGenerationStatus.RETIRED.value,
                    retired_at=retired_at,
                )
            )
            retired = await self._locked_generation(connection, identifier)
        assert retired is not None
        return _generation_from_row(retired)

    async def fail_generation(
        self,
        generation_id: str,
        failed_at: datetime,
        safe_error_code: str,
    ) -> RAGIndexGeneration:
        if not safe_error_code.strip():
            raise ValueError("safe_error_code must not be blank")
        identifier = _uuid(generation_id, "generation_id")
        async with self._transaction() as connection:
            row = await self._locked_generation(connection, identifier)
            if row is None or row["status"] != RAGIndexGenerationStatus.BUILDING.value:
                raise RAGPersistenceConflictError(
                    "only an existing building generation can fail"
                )
            await connection.execute(
                update(rag_index_generations)
                .where(rag_index_generations.c.generation_id == identifier)
                .values(
                    status=RAGIndexGenerationStatus.FAILED.value,
                    failed_at=failed_at,
                    last_safe_error_code=safe_error_code,
                )
            )
            failed = await self._locked_generation(connection, identifier)
        assert failed is not None
        return _generation_from_row(failed)

    async def delete_generation(self, generation_id: str) -> None:
        identifier = _uuid(generation_id, "generation_id")
        async with self._transaction() as connection:
            row = await self._locked_generation(connection, identifier)
            if row is None:
                return
            if row["status"] not in {
                RAGIndexGenerationStatus.RETIRED.value,
                RAGIndexGenerationStatus.FAILED.value,
            }:
                raise RAGPersistenceConflictError(
                    "only retired or failed generations can be deleted"
                )
            await connection.execute(
                delete(rag_index_generations).where(
                    rag_index_generations.c.generation_id == identifier
                )
            )

    async def latest_candidate_cutoff(self) -> RAGSourceCutoff:
        row = await self._read_one(
            select(
                transcript_items.c.created_at,
                transcript_items.c.item_id,
            )
            .select_from(
                transcript_items.join(
                    analysis_runs,
                    analysis_runs.c.run_id == transcript_items.c.run_id,
                ).join(
                    analysis_sessions,
                    and_(
                        analysis_sessions.c.run_id == transcript_items.c.run_id,
                        analysis_sessions.c.session_id == transcript_items.c.session_id,
                    ),
                )
            )
            .where(*self._candidate_predicates())
            .order_by(
                transcript_items.c.created_at.desc(),
                transcript_items.c.item_id.desc(),
            )
            .limit(1)
        )
        if row is None:
            return RAGSourceCutoff(
                created_at=datetime(1970, 1, 1, tzinfo=UTC),
                item_id=UUID(int=0),
            )
        return RAGSourceCutoff(
            created_at=row["created_at"],
            item_id=row["item_id"],
        )

    async def discover_jobs(
        self,
        generation_id: str,
        *,
        through: RAGSourceCutoff | None,
        limit: int,
        discovered_at: datetime,
    ) -> tuple[RAGIndexJob, ...]:
        if type(limit) is not int or limit < 1:
            raise ValueError("discovery limit must be a positive integer")
        identifier = _uuid(generation_id, "generation_id")
        join_condition = and_(
            rag_index_jobs.c.generation_id == identifier,
            rag_index_jobs.c.source_result_item_id == transcript_items.c.item_id,
            rag_index_jobs.c.projection_schema_version == 1,
        )
        predicates = [*self._candidate_predicates(), rag_index_jobs.c.job_id.is_(None)]
        if through is not None:
            predicates.append(
                tuple_(transcript_items.c.created_at, transcript_items.c.item_id)
                <= tuple_(through.created_at, through.item_id)
            )
        async with self._transaction() as connection:
            await self._require_writable_generation(connection, identifier)
            rows = tuple(
                (
                    await connection.execute(
                        select(
                            transcript_items.c.run_id,
                            transcript_items.c.session_id,
                            transcript_items.c.item_id,
                            transcript_items.c.created_at,
                        )
                        .select_from(
                            transcript_items.join(
                                analysis_runs,
                                analysis_runs.c.run_id == transcript_items.c.run_id,
                            )
                            .join(
                                analysis_sessions,
                                and_(
                                    analysis_sessions.c.run_id
                                    == transcript_items.c.run_id,
                                    analysis_sessions.c.session_id
                                    == transcript_items.c.session_id,
                                ),
                            )
                            .outerjoin(rag_index_jobs, join_condition)
                        )
                        .where(*predicates)
                        .order_by(
                            transcript_items.c.created_at,
                            transcript_items.c.item_id,
                        )
                        .limit(limit)
                    )
                ).mappings()
            )
            if not rows:
                return ()
            for row in rows:
                job = RAGIndexJob(
                    job_id=str(uuid4()),
                    generation_id=generation_id,
                    source_run_id=str(row["run_id"]),
                    source_session_id=str(row["session_id"]),
                    source_result_item_id=str(row["item_id"]),
                    status=RAGIndexJobStatus.PENDING,
                    attempts=0,
                    created_at=discovered_at,
                    updated_at=discovered_at,
                )
                await connection.execute(
                    postgresql_insert(rag_index_jobs)
                    .values(**self._job_values(job))
                    .on_conflict_do_nothing(constraint="uq_rag_jobs_identity")
                )
            item_ids = tuple(row["item_id"] for row in rows)
            persisted = tuple(
                (
                    await connection.execute(
                        select(rag_index_jobs)
                        .where(
                            rag_index_jobs.c.generation_id == identifier,
                            rag_index_jobs.c.source_result_item_id.in_(item_ids),
                            rag_index_jobs.c.projection_schema_version == 1,
                        )
                        .order_by(
                            rag_index_jobs.c.created_at,
                            rag_index_jobs.c.job_id,
                        )
                    )
                ).mappings()
            )
        return tuple(_job_from_row(row) for row in persisted)

    async def enqueue(self, job: RAGIndexJob) -> RAGIndexJob:
        if job.status is not RAGIndexJobStatus.PENDING:
            raise ValueError("new RAG jobs must be pending")
        values = self._job_values(job)
        statement = (
            postgresql_insert(rag_index_jobs)
            .values(**values)
            .on_conflict_do_nothing(
                constraint="uq_rag_jobs_identity",
            )
        )
        async with self._transaction() as connection:
            await self._require_writable_generation(
                connection,
                values["generation_id"],
            )
            await connection.execute(statement)
            row = (
                await connection.execute(
                    select(rag_index_jobs).where(
                        and_(
                            rag_index_jobs.c.generation_id
                            == values["generation_id"],
                            rag_index_jobs.c.source_result_item_id
                            == values["source_result_item_id"],
                            rag_index_jobs.c.projection_schema_version
                            == values["projection_schema_version"],
                        )
                    )
                )
            ).mappings().one()
        persisted = _job_from_row(row)
        if (
            persisted.source_run_id != job.source_run_id
            or persisted.source_session_id != job.source_session_id
        ):
            raise RAGPersistenceConflictError(
                "existing job identity belongs to a different source scope"
            )
        return persisted

    async def claim(
        self,
        generation_id: str,
        lease_owner: str,
        lease_until: datetime,
        limit: int,
    ) -> tuple[RAGIndexJob, ...]:
        if not lease_owner.strip():
            raise ValueError("lease_owner must not be blank")
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be a positive integer")
        now = datetime.now(UTC)
        if lease_until <= now:
            raise ValueError("lease_until must be in the future")
        identifier = _uuid(generation_id, "generation_id")
        async with self._transaction() as connection:
            await self._require_writable_generation(connection, identifier)
            rows = tuple(
                (
                    await connection.execute(
                        select(rag_index_jobs)
                        .where(
                            rag_index_jobs.c.generation_id == identifier,
                            rag_index_jobs.c.attempts < 5,
                            or_(
                                rag_index_jobs.c.status
                                == RAGIndexJobStatus.PENDING.value,
                                and_(
                                    rag_index_jobs.c.status
                                    == RAGIndexJobStatus.RETRY_WAIT.value,
                                    rag_index_jobs.c.next_attempt_at <= now,
                                ),
                                and_(
                                    rag_index_jobs.c.status
                                    == RAGIndexJobStatus.LEASED.value,
                                    rag_index_jobs.c.lease_until <= now,
                                ),
                            ),
                        )
                        .order_by(
                            rag_index_jobs.c.created_at,
                            rag_index_jobs.c.job_id,
                        )
                        .with_for_update(skip_locked=True)
                        .limit(limit)
                    )
                ).mappings()
            )
            claimed: list[RAGIndexJob] = []
            for row in rows:
                updated = (
                    await connection.execute(
                        update(rag_index_jobs)
                        .where(rag_index_jobs.c.job_id == row["job_id"])
                        .values(
                            status=RAGIndexJobStatus.LEASED.value,
                            attempts=row["attempts"] + 1,
                            next_attempt_at=None,
                            lease_owner=lease_owner,
                            lease_until=lease_until,
                            updated_at=now,
                            completed_at=None,
                        )
                        .returning(rag_index_jobs)
                    )
                ).mappings().one()
                claimed.append(_job_from_row(updated))
        return tuple(claimed)

    async def save(self, job: RAGIndexJob) -> None:
        identifier = _uuid(job.job_id, "job_id")
        values = self._job_values(job)
        async with self._transaction() as connection:
            await self._require_writable_generation(
                connection,
                values["generation_id"],
            )
            row = (
                await connection.execute(
                    select(rag_index_jobs)
                    .where(rag_index_jobs.c.job_id == identifier)
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if row is None:
                raise RAGPersistenceConflictError("RAG job does not exist")
            persisted = _job_from_row(row)
            self._validate_job_update(persisted, job)
            await connection.execute(
                update(rag_index_jobs)
                .where(rag_index_jobs.c.job_id == identifier)
                .values(**values)
            )

    async def counts(self, generation_id: str) -> RAGJobCounts:
        identifier = _uuid(generation_id, "generation_id")
        rows = await self._read_all(
            select(rag_index_jobs.c.status, func.count().label("count"))
            .where(rag_index_jobs.c.generation_id == identifier)
            .group_by(rag_index_jobs.c.status)
        )
        values = {row["status"]: row["count"] for row in rows}
        return RAGJobCounts(**values)

    async def retry_failed_jobs(
        self,
        generation_id: str,
        safe_error_codes: tuple[str, ...],
        limit: int,
        retried_at: datetime,
    ) -> tuple[RAGIndexJob, ...]:
        if not safe_error_codes:
            raise ValueError("safe_error_codes must not be empty")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        identifier = _uuid(generation_id, "generation_id")
        async with self._transaction() as connection:
            generation = await self._locked_generation(connection, identifier)
            if (
                generation is None
                or generation["status"]
                != RAGIndexGenerationStatus.BUILDING.value
            ):
                raise RAGPersistenceConflictError(
                    "failed jobs can only be retried for a building generation"
                )
            rows = tuple(
                (
                    await connection.execute(
                        select(rag_index_jobs)
                        .where(
                            rag_index_jobs.c.generation_id == identifier,
                            rag_index_jobs.c.status
                            == RAGIndexJobStatus.FAILED.value,
                            rag_index_jobs.c.last_safe_error_code.in_(
                                safe_error_codes
                            ),
                        )
                        .order_by(
                            rag_index_jobs.c.updated_at,
                            rag_index_jobs.c.job_id,
                        )
                        .with_for_update(skip_locked=True)
                        .limit(limit)
                    )
                ).mappings()
            )
            retried: list[RAGIndexJob] = []
            for row in rows:
                updated = (
                    await connection.execute(
                        update(rag_index_jobs)
                        .where(rag_index_jobs.c.job_id == row["job_id"])
                        .values(
                            status=RAGIndexJobStatus.PENDING.value,
                            attempts=0,
                            next_attempt_at=None,
                            lease_owner=None,
                            lease_until=None,
                            last_safe_error_code=None,
                            updated_at=max(
                                retried_at,
                                row["updated_at"] + timedelta(microseconds=1),
                            ),
                            completed_at=None,
                        )
                        .returning(rag_index_jobs)
                    )
                ).mappings().one()
                retried.append(_job_from_row(updated))
        return tuple(retried)

    async def write_ready_projection(
        self,
        job: RAGIndexJob,
        case: RAGCaseProjectionV1,
        fragments: tuple[RAGFragmentProjectionV1, ...],
    ) -> None:
        self._validate_projection_scope(job, case, fragments)
        windows = self._window_rows(case)
        generation_id = _uuid(case.case_ref.generation_id, "generation_id")
        case_id = _uuid(case.case_ref.case_id, "case_id")
        now = datetime.now(UTC)
        try:
            async with self._transaction() as connection:
                await self._require_writable_generation(connection, generation_id)
                existing = (
                    await connection.execute(
                        select(rag_case_projections.c.projection_digest).where(
                            rag_case_projections.c.generation_id == generation_id,
                            rag_case_projections.c.case_id == case_id,
                        )
                    )
                ).mappings().one_or_none()
                if existing is not None:
                    await self._validate_idempotent_projection(
                        connection, case, fragments, existing["projection_digest"]
                    )
                    return
                await connection.execute(
                    insert(rag_case_projections).values(
                        generation_id=generation_id,
                        case_id=case_id,
                        profile_id=case.profile_id,
                        embedding_dimensions=len(case.embedding.values),
                        source_run_id=_uuid(case.case_ref.run_id, "source_run_id"),
                        source_session_id=_uuid(
                            case.case_ref.session_id, "source_session_id"
                        ),
                        source_result_item_id=_uuid(
                            case.case_ref.result_item_id, "source_result_item_id"
                        ),
                        analysis_context_item_id=_uuid(
                            case.case_ref.analysis_context_item_id,
                            "analysis_context_item_id",
                        ),
                        target_comid=case.case_ref.target_comid,
                        projection_schema_version=case.projection_schema_version,
                        source_content_type=case.source_content_type,
                        source_content_schema_version=case.source_content_schema_version,
                        mode=case.mode.value,
                        parameter_keys=[
                            _parameter_key_text(item) for item in case.parameter_keys
                        ],
                        window_ids=list(case.window_ids),
                        source_completed_at=case.source_completed_at,
                        canonical_query=case.canonical_query,
                        canonical_query_digest=case.canonical_query_digest,
                        search_document=func.to_tsvector("simple", case.canonical_query),
                        embedding=list(case.embedding.values),
                        result_projection=case.result_projection.model_dump(mode="json"),
                        projection_digest=case.projection_digest,
                        ready_at=now,
                    )
                )
                await connection.execute(insert(rag_case_windows), windows)
                if fragments:
                    await connection.execute(
                        insert(rag_fragment_projections),
                        [self._fragment_values(fragment) for fragment in fragments],
                    )
                updated = await connection.execute(
                    update(rag_index_jobs)
                    .where(
                        rag_index_jobs.c.job_id == _uuid(job.job_id, "job_id"),
                        rag_index_jobs.c.status == RAGIndexJobStatus.LEASED.value,
                        rag_index_jobs.c.lease_owner == job.lease_owner,
                    )
                    .values(
                        status=RAGIndexJobStatus.COMPLETED.value,
                        lease_owner=None,
                        lease_until=None,
                        next_attempt_at=None,
                        last_safe_error_code=None,
                        updated_at=now,
                        completed_at=now,
                    )
                )
                if updated.rowcount != 1:
                    raise RAGPersistenceConflictError(
                        "projection job is not leased by the expected owner"
                    )
        except IntegrityError as error:
            raise RAGPersistenceConflictError(
                "projection violates generation or source ownership constraints"
            ) from error

    async def append_consumption_link(
        self,
        link: RAGConsumptionLinkRecord,
    ) -> RAGConsumptionLinkRecord:
        values = link.model_dump(mode="python")
        statement = (
            postgresql_insert(rag_consumption_links)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_rag_consumption_identity")
        )
        try:
            async with self._transaction() as connection:
                row = (
                    await connection.execute(
                        select(rag_consumption_links).where(
                            rag_consumption_links.c.source_generation_id
                            == link.source_generation_id,
                            rag_consumption_links.c.source_case_id
                            == link.source_case_id,
                            rag_consumption_links.c.consumer_item_id
                            == link.consumer_item_id,
                        )
                    )
                ).mappings().one_or_none()
                if row is None:
                    if not await has_valid_consumption_source(
                        connection,
                        source_generation_id=link.source_generation_id,
                        source_case_id=link.source_case_id,
                        source_run_id=link.source_run_id,
                        source_session_id=link.source_session_id,
                        source_result_item_id=link.source_result_item_id,
                    ):
                        raise RAGPersistenceConflictError(
                            "consumption link has no valid source transcript"
                        )
                    await connection.execute(statement)
                    row = (
                        await connection.execute(
                            select(rag_consumption_links).where(
                                rag_consumption_links.c.source_generation_id
                                == link.source_generation_id,
                                rag_consumption_links.c.source_case_id
                                == link.source_case_id,
                                rag_consumption_links.c.consumer_item_id
                                == link.consumer_item_id,
                            )
                        )
                    ).mappings().one()
        except IntegrityError as error:
            raise RAGPersistenceConflictError(
                "consumption link has no valid source or consumer item"
            ) from error
        persisted = RAGConsumptionLinkRecord.model_validate(dict(row))
        if persisted != link:
            raise RAGPersistenceConflictError(
                "existing consumption identity has different ownership fields"
            )
        return persisted

    async def read_consumption_links(
        self,
        consumer_run_id: UUID,
    ) -> tuple[RAGConsumptionLinkRecord, ...]:
        rows = await self._read_all(
            select(rag_consumption_links)
            .where(rag_consumption_links.c.consumer_run_id == consumer_run_id)
            .order_by(
                rag_consumption_links.c.created_at,
                rag_consumption_links.c.link_id,
            )
        )
        return tuple(
            RAGConsumptionLinkRecord.model_validate(dict(row)) for row in rows
        )

    async def find_source_delete_conflicts(
        self,
        source_run_ids: tuple[UUID, ...],
    ) -> tuple[RAGConsumptionLinkRecord, ...]:
        if not source_run_ids:
            return ()
        rows = await self._read_all(
            select(rag_consumption_links)
            .where(
                rag_consumption_links.c.source_run_id.in_(source_run_ids),
                rag_consumption_links.c.consumer_run_id.not_in(source_run_ids),
            )
            .order_by(
                rag_consumption_links.c.created_at,
                rag_consumption_links.c.link_id,
            )
        )
        return tuple(
            RAGConsumptionLinkRecord.model_validate(dict(row)) for row in rows
        )

    async def inspect_cleanup_dependencies(
        self,
        run_ids: tuple[str, ...],
    ) -> RAGCleanupPlan:
        requested = tuple(_uuid(value, "run_id") for value in run_ids)
        if not requested:
            raise ValueError("run_ids must not be empty")
        if len(requested) != len(set(requested)):
            raise ValueError("run_ids must not contain duplicates")
        async with self._transaction() as connection:
            return await self._build_cleanup_plan(connection, requested)

    async def delete_history_runs(
        self,
        run_ids: tuple[str, ...],
        *,
        cascade_consumers: bool,
    ) -> RAGCleanupResult:
        requested = tuple(_uuid(value, "run_id") for value in run_ids)
        if not requested:
            raise ValueError("run_ids must not be empty")
        if len(requested) != len(set(requested)):
            raise ValueError("run_ids must not contain duplicates")
        try:
            async with self._transaction() as connection:
                plan = await self._build_cleanup_plan(connection, requested)
                if plan.cascade_required and not cascade_consumers:
                    raise RAGPersistenceConflictError(
                        "source history is referenced by retained consumer runs"
                    )
                target_ids = tuple(
                    _uuid(value, "delete_run_id")
                    for value in plan.delete_run_ids
                )
                rows = tuple(
                    (
                        await connection.execute(
                            select(
                                analysis_runs.c.run_id,
                                analysis_runs.c.status,
                                analysis_runs.c.idempotency_key,
                                analysis_runs.c.request_fingerprint,
                            )
                            .where(analysis_runs.c.run_id.in_(target_ids))
                            .order_by(analysis_runs.c.run_id)
                            .with_for_update()
                        )
                    ).mappings()
                )
                nonterminal = tuple(
                    row
                    for row in rows
                    if row["status"]
                    not in {
                        "completed",
                        "failed",
                        "cancelled",
                        "completed_with_persistence_error",
                    }
                )
                if nonterminal:
                    raise RAGPersistenceConflictError(
                        "history cleanup requires terminal runs"
                    )
                existing_ids = {row["run_id"] for row in rows}
                delete_order = tuple(
                    value
                    for value in plan.delete_order
                    if _uuid(value, "delete_run_id") in existing_ids
                )
                if not delete_order:
                    return RAGCleanupResult(
                        requested_run_ids=tuple(str(value) for value in requested),
                        deleted_run_ids=(),
                        delete_order=(),
                    )
                delete_ids = tuple(
                    _uuid(value, "delete_run_id") for value in delete_order
                )
                item_ids = select(transcript_items.c.item_id).where(
                    transcript_items.c.run_id.in_(delete_ids)
                )
                await connection.execute(
                    delete(rag_consumption_links).where(
                        rag_consumption_links.c.consumer_run_id.in_(delete_ids)
                    )
                )
                await connection.execute(
                    delete(transcript_contents).where(
                        transcript_contents.c.item_id.in_(item_ids)
                    )
                )
                await connection.execute(
                    delete(transcript_items).where(
                        transcript_items.c.run_id.in_(delete_ids)
                    )
                )
                await connection.execute(
                    delete(analysis_sessions).where(
                        analysis_sessions.c.run_id.in_(delete_ids)
                    )
                )
                await connection.execute(
                    delete(analysis_runs).where(
                        analysis_runs.c.run_id.in_(delete_ids)
                    )
                )
                tombstones = [
                    {
                        "idempotency_key": row["idempotency_key"],
                        "request_fingerprint": row["request_fingerprint"],
                        "deleted_run_id": row["run_id"],
                        "deleted_at": datetime.now(UTC),
                    }
                    for row in rows if row["idempotency_key"] is not None
                ]
                if tombstones:
                    await connection.execute(
                        insert(analysis_run_idempotency_tombstones), tombstones
                    )
                return RAGCleanupResult(
                    requested_run_ids=tuple(str(value) for value in requested),
                    deleted_run_ids=delete_order,
                    delete_order=delete_order,
                )
        except IntegrityError as error:
            raise RAGPersistenceConflictError(
                "history cleanup raced with a new consumption dependency"
            ) from error

    @staticmethod
    async def _build_cleanup_plan(
        connection: AsyncConnection,
        requested: tuple[UUID, ...],
    ) -> RAGCleanupPlan:
        requested_set = set(requested)
        seen = set(requested)
        frontier = set(requested)
        graph: dict[UUID, set[UUID]] = {}
        dependencies: dict[UUID, RAGCleanupDependency] = {}
        while frontier:
            rows = tuple(
                (
                    await connection.execute(
                        select(
                            rag_consumption_links.c.link_id,
                            rag_consumption_links.c.source_generation_id,
                            rag_consumption_links.c.source_case_id,
                            rag_consumption_links.c.source_run_id,
                            rag_consumption_links.c.source_result_item_id,
                            rag_consumption_links.c.consumer_run_id,
                            rag_consumption_links.c.consumer_item_id,
                        )
                        .where(
                            rag_consumption_links.c.source_run_id.in_(
                                tuple(frontier)
                            )
                        )
                        .order_by(
                            rag_consumption_links.c.created_at,
                            rag_consumption_links.c.link_id,
                        )
                    )
                ).mappings()
            )
            next_frontier: set[UUID] = set()
            for row in rows:
                dependencies[row["link_id"]] = RAGCleanupDependency(
                    link_id=str(row["link_id"]),
                    source_generation_id=str(row["source_generation_id"]),
                    source_case_id=str(row["source_case_id"]),
                    source_run_id=str(row["source_run_id"]),
                    source_result_item_id=str(row["source_result_item_id"]),
                    consumer_run_id=str(row["consumer_run_id"]),
                    consumer_item_id=str(row["consumer_item_id"]),
                )
                consumer = row["consumer_run_id"]
                graph.setdefault(row["source_run_id"], set()).add(consumer)
                if consumer not in seen:
                    next_frontier.add(consumer)
            if not next_frontier:
                break
            seen.update(next_frontier)
            frontier = next_frontier
        ordered_ids: list[UUID] = []
        visited: set[UUID] = set()
        visiting: set[UUID] = set()

        def add_consumers_before_source(run_id: UUID) -> None:
            if run_id in visited or run_id in visiting:
                return
            visiting.add(run_id)
            for consumer_run_id in sorted(graph.get(run_id, ())):
                add_consumers_before_source(consumer_run_id)
            visiting.remove(run_id)
            visited.add(run_id)
            ordered_ids.append(run_id)

        for run_id in sorted(requested):
            add_consumers_before_source(run_id)
        for run_id in sorted(seen):
            add_consumers_before_source(run_id)
        delete_order = tuple(str(run_id) for run_id in ordered_ids)
        return RAGCleanupPlan(
            requested_run_ids=tuple(str(value) for value in requested),
            delete_run_ids=tuple(str(run_id) for run_id in sorted(seen)),
            delete_order=delete_order,
            dependencies=tuple(dependencies.values()),
            cascade_required=bool(seen.difference(requested_set)),
        )

    @staticmethod
    def _generation_values(generation: RAGIndexGeneration) -> dict[str, object]:
        profile = generation.profile
        return {
            "generation_id": _uuid(generation.generation_id, "generation_id"),
            "profile_id": profile.profile_id,
            "provider": profile.provider,
            "model": profile.model,
            "output_dimensions": profile.output_dimensions,
            "max_input_tokens": profile.max_input_tokens,
            "distance_metric": profile.distance_metric.value,
            "semantic_min_similarity": profile.semantic_min_similarity,
            "canonical_query_schema_version": profile.canonical_query_schema_version,
            "canonical_template_version": profile.canonical_template_version,
            "projection_schema_version": profile.projection_schema_version,
            "profile_digest": profile.profile_digest,
            "status": generation.status.value,
            "source_cutoff_created_at": generation.source_cutoff_created_at,
            "source_cutoff_item_id": _uuid(
                generation.source_cutoff_item_id, "source_cutoff_item_id"
            ),
            "created_at": generation.created_at,
            "activated_at": generation.activated_at,
            "retired_at": generation.retired_at,
            "failed_at": generation.failed_at,
            "last_safe_error_code": generation.last_safe_error_code,
        }

    @staticmethod
    def _candidate_predicates() -> tuple:
        return (
            analysis_runs.c.status == "completed",
            analysis_sessions.c.status == "completed",
            analysis_sessions.c.agent_role == "waterbody",
            transcript_items.c.session_id.is_not(None),
            transcript_items.c.content_type == "inference_result",
            transcript_items.c.content_schema_version == 1,
            transcript_items.c.rag_eligible.is_(True),
        )

    @staticmethod
    def _job_values(job: RAGIndexJob) -> dict[str, object]:
        return {
            "job_id": _uuid(job.job_id, "job_id"),
            "generation_id": _uuid(job.generation_id, "generation_id"),
            "source_run_id": _uuid(job.source_run_id, "source_run_id"),
            "source_session_id": _uuid(job.source_session_id, "source_session_id"),
            "source_result_item_id": _uuid(
                job.source_result_item_id, "source_result_item_id"
            ),
            "projection_schema_version": job.projection_schema_version,
            "status": job.status.value,
            "attempts": job.attempts,
            "next_attempt_at": job.next_attempt_at,
            "lease_owner": job.lease_owner,
            "lease_until": job.lease_until,
            "last_safe_error_code": job.last_safe_error_code,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "completed_at": job.completed_at,
        }

    @staticmethod
    def _validate_job_update(persisted: RAGIndexJob, submitted: RAGIndexJob) -> None:
        immutable = (
            "generation_id",
            "source_run_id",
            "source_session_id",
            "source_result_item_id",
            "projection_schema_version",
            "created_at",
        )
        if any(getattr(persisted, field) != getattr(submitted, field) for field in immutable):
            raise RAGPersistenceConflictError("RAG job immutable identity changed")
        if submitted.attempts < persisted.attempts:
            raise RAGPersistenceConflictError("RAG job attempts cannot decrease")
        if submitted == persisted:
            return
        if submitted.updated_at <= persisted.updated_at:
            raise RAGPersistenceConflictError("stale RAG job update")
        if persisted.status is RAGIndexJobStatus.LEASED:
            if submitted.lease_owner is not None and (
                submitted.lease_owner != persisted.lease_owner
            ):
                raise RAGPersistenceConflictError("RAG job lease owner changed")
        elif submitted.status is not persisted.status:
            raise RAGPersistenceConflictError(
                "only a leased RAG job may transition through save"
            )

    @staticmethod
    def _validate_projection_scope(
        job: RAGIndexJob,
        case: RAGCaseProjectionV1,
        fragments: tuple[RAGFragmentProjectionV1, ...],
    ) -> None:
        reference = case.case_ref
        if job.status is not RAGIndexJobStatus.LEASED:
            raise ValueError("projection writes require a leased job")
        if (
            job.generation_id != reference.generation_id
            or job.source_run_id != reference.run_id
            or job.source_session_id != reference.session_id
            or job.source_result_item_id != reference.result_item_id
            or job.projection_schema_version != case.projection_schema_version
        ):
            raise ValueError("job and case projection identities do not match")
        if len(case.embedding.values) < 1:
            raise ValueError("case embedding must not be empty")
        if set(case.window_ids) != {
            cell.window_id for cell in case.result_projection.cells
        }:
            raise ValueError("case windows must match result projection windows")
        if set(case.parameter_keys) != {
            cell.parameter for cell in case.result_projection.cells
        }:
            raise ValueError("case parameters must match result projection parameters")
        identities: set[tuple[str, ParameterKey, str]] = set()
        result_cells = {
            (cell.parameter, cell.window_id)
            for cell in case.result_projection.cells
        }
        for fragment in fragments:
            if fragment.case_ref != reference:
                raise ValueError("fragment belongs to a different case")
            identity = (
                fragment.source_fragment_item_id,
                fragment.parameter,
                fragment.window_id,
            )
            if identity in identities:
                raise ValueError("duplicate source fragment-cell projection")
            identities.add(identity)
            if (fragment.parameter, fragment.window_id) not in result_cells:
                raise ValueError("fragment cell is outside case projection")

    @staticmethod
    def _window_rows(case: RAGCaseProjectionV1) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for window in case.windows:
            if len(window.season_buckets) != 1:
                raise ValueError("persisted case windows require one season bucket")
            rows.append(
                {
                    "generation_id": _uuid(
                        case.case_ref.generation_id, "generation_id"
                    ),
                    "case_id": _uuid(case.case_ref.case_id, "case_id"),
                    "window_id": window.window_id,
                    "time_range": Range(
                        window.time_range.start,
                        window.time_range.end,
                        "[)",
                    ),
                    "local_months": list(window.local_months),
                    "season_bucket": window.season_buckets[0].value,
                    "season_year": window.season_year,
                    "duration_seconds": window.duration_seconds,
                    "resolution": window.resolution.value,
                    "parameter_keys": sorted(
                        {_parameter_key_text(item) for item in window.parameter_keys}
                    ),
                }
            )
        return rows

    @staticmethod
    def _fragment_values(fragment: RAGFragmentProjectionV1) -> dict[str, object]:
        return {
            "generation_id": _uuid(fragment.case_ref.generation_id, "generation_id"),
            "case_id": _uuid(fragment.case_ref.case_id, "case_id"),
            "source_fragment_item_id": _uuid(
                fragment.source_fragment_item_id, "source_fragment_item_id"
            ),
            "source_run_id": _uuid(fragment.case_ref.run_id, "source_run_id"),
            "source_session_id": _uuid(
                fragment.case_ref.session_id, "source_session_id"
            ),
            "evidence_id": fragment.evidence_id,
            "projection_schema_version": fragment.projection_schema_version,
            "source_content_type": fragment.source_content_type,
            "source_content_schema_version": fragment.source_content_schema_version,
            "parameter_key": _parameter_key_text(fragment.parameter),
            "window_id": fragment.window_id,
            "valid_time_range": Range(
                fragment.valid_time_range.start,
                fragment.valid_time_range.end,
                "[)",
            ),
            "quality_tier": fragment.quality_tier.value,
            "applicability": fragment.applicability,
            "provenance_identity": fragment.provenance_identity,
            "safe_projection": fragment.model_dump(mode="json"),
            "projection_digest": fragment.projection_digest,
        }

    @staticmethod
    async def _validate_idempotent_projection(
        connection: AsyncConnection,
        case: RAGCaseProjectionV1,
        fragments: tuple[RAGFragmentProjectionV1, ...],
        stored_case_digest: str,
    ) -> None:
        if stored_case_digest != case.projection_digest:
            raise RAGPersistenceConflictError(
                "existing case identity has a different projection digest"
            )
        rows = tuple(
            (
                await connection.execute(
                    select(
                        rag_fragment_projections.c.source_fragment_item_id,
                        rag_fragment_projections.c.parameter_key,
                        rag_fragment_projections.c.window_id,
                        rag_fragment_projections.c.projection_digest,
                    ).where(
                        rag_fragment_projections.c.generation_id
                        == _uuid(case.case_ref.generation_id, "generation_id"),
                        rag_fragment_projections.c.case_id
                        == _uuid(case.case_ref.case_id, "case_id"),
                    )
                )
            ).mappings()
        )
        stored = {
            (
                str(row["source_fragment_item_id"]),
                row["parameter_key"],
                row["window_id"],
            ): row["projection_digest"]
            for row in rows
        }
        submitted = {
            (
                fragment.source_fragment_item_id,
                _parameter_key_text(fragment.parameter),
                fragment.window_id,
            ): fragment.projection_digest
            for fragment in fragments
        }
        if stored != submitted:
            raise RAGPersistenceConflictError(
                "existing case identity has different fragment projections"
            )

    async def _locked_generation(
        self,
        connection: AsyncConnection,
        generation_id: UUID,
    ):
        return (
            await connection.execute(
                select(rag_index_generations)
                .where(rag_index_generations.c.generation_id == generation_id)
                .with_for_update()
            )
        ).mappings().one_or_none()

    async def _require_writable_generation(
        self,
        connection: AsyncConnection,
        generation_id: UUID,
    ):
        row = await self._locked_generation(connection, generation_id)
        if row is None or row["status"] not in {
            RAGIndexGenerationStatus.BUILDING.value,
            RAGIndexGenerationStatus.ACTIVE.value,
        }:
            raise RAGPersistenceConflictError(
                "RAG jobs and projections require a building or active generation"
            )
        return row

    @staticmethod
    async def _validate_generation_ready_for_activation(
        connection: AsyncConnection,
        generation,
    ) -> None:
        generation_id = generation["generation_id"]
        cutoff = tuple_(
            generation["source_cutoff_created_at"],
            generation["source_cutoff_item_id"],
        )
        source_position = tuple_(
            transcript_items.c.created_at,
            transcript_items.c.item_id,
        )
        candidate_base = (
            transcript_items.join(
                analysis_runs,
                analysis_runs.c.run_id == transcript_items.c.run_id,
            ).join(
                analysis_sessions,
                and_(
                    analysis_sessions.c.run_id == transcript_items.c.run_id,
                    analysis_sessions.c.session_id == transcript_items.c.session_id,
                ),
            )
        )
        candidate_count = await connection.scalar(
            select(func.count())
            .select_from(candidate_base)
            .where(
                *PostgresRAGRepository._candidate_predicates(),
                source_position <= cutoff,
            )
        )
        missing_candidate_jobs = await connection.scalar(
            select(func.count())
            .select_from(
                candidate_base.outerjoin(
                    rag_index_jobs,
                    and_(
                        rag_index_jobs.c.generation_id == generation_id,
                        rag_index_jobs.c.source_result_item_id
                        == transcript_items.c.item_id,
                        rag_index_jobs.c.projection_schema_version
                        == generation["projection_schema_version"],
                    ),
                )
            )
            .where(
                *PostgresRAGRepository._candidate_predicates(),
                source_position <= cutoff,
                rag_index_jobs.c.job_id.is_(None),
            )
        )
        total_jobs = await connection.scalar(
            select(func.count())
            .select_from(rag_index_jobs)
            .where(rag_index_jobs.c.generation_id == generation_id)
        )
        job_source = transcript_items.alias("rag_activation_job_source")
        invalid_job_sources = await connection.scalar(
            select(func.count())
            .select_from(
                rag_index_jobs.outerjoin(
                    job_source,
                    and_(
                        job_source.c.run_id == rag_index_jobs.c.source_run_id,
                        job_source.c.session_id == rag_index_jobs.c.source_session_id,
                        job_source.c.item_id == rag_index_jobs.c.source_result_item_id,
                    ),
                )
            )
            .where(
                rag_index_jobs.c.generation_id == generation_id,
                or_(
                    job_source.c.item_id.is_(None),
                    tuple_(job_source.c.created_at, job_source.c.item_id) > cutoff,
                    job_source.c.content_type != "inference_result",
                    job_source.c.content_schema_version != 1,
                    rag_index_jobs.c.projection_schema_version
                    != generation["projection_schema_version"],
                    and_(
                        rag_index_jobs.c.status == RAGIndexJobStatus.EXCLUDED.value,
                        rag_index_jobs.c.last_safe_error_code
                        != "source_no_longer_eligible",
                    ),
                ),
            )
        )
        incomplete_jobs = await connection.scalar(
            select(func.count())
            .select_from(rag_index_jobs)
            .where(
                rag_index_jobs.c.generation_id == generation_id,
                rag_index_jobs.c.status.not_in(
                    (
                        RAGIndexJobStatus.COMPLETED.value,
                        RAGIndexJobStatus.EXCLUDED.value,
                    )
                ),
            )
        )
        completed_jobs = await connection.scalar(
            select(func.count())
            .select_from(rag_index_jobs)
            .where(
                rag_index_jobs.c.generation_id == generation_id,
                rag_index_jobs.c.status == RAGIndexJobStatus.COMPLETED.value,
            )
        )

        source_item = transcript_items.alias("rag_activation_source_item")
        context_item = transcript_items.alias("rag_activation_context_item")
        window_count = (
            select(func.count())
            .select_from(rag_case_windows)
            .where(
                rag_case_windows.c.generation_id
                == rag_case_projections.c.generation_id,
                rag_case_windows.c.case_id == rag_case_projections.c.case_id,
            )
            .correlate(rag_case_projections)
            .scalar_subquery()
        )
        case_rows = await connection.execute(
            select(
                func.count().label("case_count"),
                func.count()
                .filter(
                    and_(
                        rag_index_jobs.c.job_id.is_not(None),
                        rag_index_jobs.c.status
                        == RAGIndexJobStatus.COMPLETED.value,
                        rag_index_jobs.c.projection_schema_version
                        == generation["projection_schema_version"],
                        rag_case_projections.c.profile_id == generation["profile_id"],
                        rag_case_projections.c.embedding_dimensions
                        == generation["output_dimensions"],
                        func.public.vector_dims(rag_case_projections.c.embedding)
                        == generation["output_dimensions"],
                        rag_case_projections.c.source_content_type
                        == "inference_result",
                        rag_case_projections.c.source_content_schema_version == 1,
                        rag_case_projections.c.canonical_query_digest.op("~")(
                            r"^sha256:[0-9a-f]{64}$"
                        ),
                        rag_case_projections.c.projection_digest.op("~")(
                            r"^sha256:[0-9a-f]{64}$"
                        ),
                        source_item.c.content_type == "inference_result",
                        source_item.c.content_schema_version == 1,
                        source_item.c.rag_eligible.is_(True),
                        context_item.c.content_type == "query_understood",
                        context_item.c.content_schema_version == 1,
                        analysis_runs.c.status == "completed",
                        analysis_sessions.c.status == "completed",
                        window_count
                        == func.cardinality(rag_case_projections.c.window_ids),
                    )
                )
                .label("valid_case_count"),
            )
            .select_from(
                rag_case_projections.outerjoin(
                    rag_index_jobs,
                    and_(
                        rag_index_jobs.c.generation_id
                        == rag_case_projections.c.generation_id,
                        rag_index_jobs.c.source_run_id
                        == rag_case_projections.c.source_run_id,
                        rag_index_jobs.c.source_session_id
                        == rag_case_projections.c.source_session_id,
                        rag_index_jobs.c.source_result_item_id
                        == rag_case_projections.c.source_result_item_id,
                    ),
                )
                .outerjoin(
                    source_item,
                    and_(
                        source_item.c.run_id == rag_case_projections.c.source_run_id,
                        source_item.c.session_id
                        == rag_case_projections.c.source_session_id,
                        source_item.c.item_id
                        == rag_case_projections.c.source_result_item_id,
                    ),
                )
                .outerjoin(
                    context_item,
                    and_(
                        context_item.c.run_id == rag_case_projections.c.source_run_id,
                        context_item.c.item_id
                        == rag_case_projections.c.analysis_context_item_id,
                    ),
                )
                .outerjoin(
                    analysis_runs,
                    analysis_runs.c.run_id == rag_case_projections.c.source_run_id,
                )
                .outerjoin(
                    analysis_sessions,
                    and_(
                        analysis_sessions.c.run_id
                        == rag_case_projections.c.source_run_id,
                        analysis_sessions.c.session_id
                        == rag_case_projections.c.source_session_id,
                    ),
                )
            )
            .where(rag_case_projections.c.generation_id == generation_id)
        )
        counts = case_rows.mappings().one()

        fragment_item = transcript_items.alias("rag_activation_fragment_item")
        invalid_fragments = await connection.scalar(
            select(func.count())
            .select_from(
                rag_fragment_projections.outerjoin(
                    fragment_item,
                    and_(
                        fragment_item.c.run_id
                        == rag_fragment_projections.c.source_run_id,
                        fragment_item.c.session_id
                        == rag_fragment_projections.c.source_session_id,
                        fragment_item.c.item_id
                        == rag_fragment_projections.c.source_fragment_item_id,
                    ),
                )
            )
            .where(
                rag_fragment_projections.c.generation_id == generation_id,
                or_(
                    rag_fragment_projections.c.projection_schema_version
                    != generation["projection_schema_version"],
                    rag_fragment_projections.c.source_content_type
                    != "evidence_fragment",
                    rag_fragment_projections.c.source_content_schema_version != 1,
                    ~rag_fragment_projections.c.projection_digest.op("~")(
                        r"^sha256:[0-9a-f]{64}$"
                    ),
                    fragment_item.c.item_id.is_(None),
                    fragment_item.c.content_type != "evidence_fragment",
                    fragment_item.c.content_schema_version != 1,
                    fragment_item.c.rag_eligible.is_not(True),
                ),
            )
        )
        sample = (
            select(
                rag_case_projections.c.case_id,
                rag_case_projections.c.embedding,
            )
            .where(rag_case_projections.c.generation_id == generation_id)
            .order_by(rag_case_projections.c.case_id)
            .limit(8)
            .subquery("rag_activation_sample")
        )
        invalid_sample_vectors = await connection.scalar(
            select(func.count())
            .select_from(sample)
            .where(
                or_(
                    sample.c.embedding.op("OPERATOR(public.<=>)")(
                        sample.c.embedding
                    ).is_(None),
                    func.abs(
                        sample.c.embedding.op("OPERATOR(public.<=>)")(
                            sample.c.embedding
                        )
                    )
                    > 0.000001,
                )
            )
        )
        case_count = counts["case_count"] or 0
        valid_case_count = counts["valid_case_count"] or 0
        if (
            incomplete_jobs
            or candidate_count != total_jobs
            or missing_candidate_jobs
            or invalid_job_sources
            or completed_jobs != case_count
            or valid_case_count != case_count
            or invalid_fragments
            or invalid_sample_vectors
        ):
            raise RAGPersistenceConflictError(
                "generation activation requires complete validated jobs and projections"
            )

    async def _read_one(self, statement):
        rows = await self._read_all(statement)
        if len(rows) > 1:
            raise RAGPersistenceConflictError("RAG read expected at most one row")
        return rows[0] if rows else None

    async def _read_all(self, statement) -> tuple:
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                async with self._engine.connect() as connection:
                    return tuple((await connection.execute(statement)).mappings())
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            raise PortUnavailableError(
                "RAG persistence is unavailable",
                provider="postgresql",
                operation="rag_read",
            ) from error

    @asynccontextmanager
    async def _transaction(self):
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                async with self._engine.begin() as connection:
                    yield connection
        except IntegrityError:
            raise
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            raise PortUnavailableError(
                "RAG persistence is unavailable",
                provider="postgresql",
                operation="rag_write",
            ) from error
