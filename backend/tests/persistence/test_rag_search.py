import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.domain import (
    AuthorizedRAGComid,
    AuthorizedRAGSearchQuery,
    CanonicalCaseQueryV1,
    EmbeddingVector,
    HistoricalSearchIntentV1,
    HistoricalSearchStrategy,
    HistoricalTopologyRelation,
    TimeRange,
)
from app.infrastructure.persistence.database import create_database_engine
from app.infrastructure.persistence.rag_repository import PostgresRAGRepository
from scripts.test_database import temporary_migrated_schema
from tests.persistence.rag_test_support import (
    embedding_profile,
    pending_job,
    ready_projection,
    seed_completed_source,
)


async def _stored_case(repository, engine):
    now = datetime.now(UTC)
    source = await seed_completed_source(engine)
    profile = embedding_profile()
    generation = await repository.create_building_generation(
        profile,
        source.completed_at,
        str(source.result_item_id),
    )
    await repository.enqueue(pending_job(source, generation.generation_id))
    job = (
        await repository.claim(
            generation.generation_id,
            "search-test-worker",
            now + timedelta(minutes=1),
            1,
        )
    )[0]
    projection, fragments = ready_projection(
        source,
        generation.generation_id,
        profile,
    )
    await repository.write_ready_projection(job, projection, fragments)
    active = await repository.activate_generation(
        generation.generation_id,
        now,
    )
    return active, projection


def _query(generation, projection, strategy):
    if strategy is HistoricalSearchStrategy.NEARBY_COMID_PARAMETER:
        current = "87654321"
        scope = (
            AuthorizedRAGComid(
                comid=projection.case_ref.target_comid,
                spatial_distance_km=5,
            ),
        )
        extras = {"spatial_radius_km": 25}
    elif strategy is HistoricalSearchStrategy.TOPOLOGY_NEIGHBORHOOD_PARAMETER:
        current = "87654321"
        scope = (
            AuthorizedRAGComid(
                comid=projection.case_ref.target_comid,
                topology_relation=HistoricalTopologyRelation.ANCESTOR,
                topology_hops=1,
                topology_path_distance_km=5,
            ),
        )
        extras = {"topology_max_hops": 5, "topology_max_distance_km": 25}
    else:
        current = projection.case_ref.target_comid
        scope = (AuthorizedRAGComid(comid=current),)
        extras = {}
    canonical = CanonicalCaseQueryV1.model_validate_json(projection.canonical_query)
    return AuthorizedRAGSearchQuery(
        generation_id=generation.generation_id,
        profile_id=generation.profile.profile_id,
        intent=HistoricalSearchIntentV1(
            strategy=strategy,
            parameter_keys=projection.parameter_keys,
            window_ids=projection.window_ids,
        ),
        current_comid=current,
        candidate_comids=tuple(item.comid for item in scope),
        comid_scope=scope,
        query_windows=projection.windows,
        canonical_query=canonical.canonical_text,
        canonical_query_digest=canonical.canonical_digest,
        query_embedding=(
            EmbeddingVector(values=(0.1, 0.1, 0.1))
            if strategy is HistoricalSearchStrategy.SEMANTIC_CASE_QUERY
            else None
        ),
        semantic_min_similarity=(
            generation.profile.semantic_min_similarity
            if strategy is HistoricalSearchStrategy.SEMANTIC_CASE_QUERY
            else None
        ),
        **extras,
    )


@pytest.mark.parametrize("strategy", tuple(HistoricalSearchStrategy))
def test_postgres_search_hard_filters_and_ranks_all_five_strategies(strategy) -> None:
    async def exercise(settings) -> None:
        engine = create_database_engine(settings)
        repository = PostgresRAGRepository(
            engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        try:
            generation, projection = await _stored_case(repository, engine)
            candidates = await repository.search_candidates(
                _query(generation, projection, strategy)
            )

            assert len(candidates) == 1
            assert candidates[0].case_ref == projection.case_ref
            assert candidates[0].matched_by.strategy is strategy
            assert 0 <= candidates[0].rag_score <= 1
        finally:
            await engine.dispose()

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_postgres_exact_search_rejects_non_overlapping_window_in_sql() -> None:
    async def exercise(settings) -> None:
        engine = create_database_engine(settings)
        repository = PostgresRAGRepository(
            engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        try:
            generation, projection = await _stored_case(repository, engine)
            query = _query(
                generation,
                projection,
                HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW,
            )
            shifted = projection.windows[0].model_copy(
                update={
                    "window_id": "non-overlapping-current-window",
                    "time_range": TimeRange(
                        start=datetime(2027, 7, 10, tzinfo=UTC),
                        end=datetime(2027, 7, 17, tzinfo=UTC),
                    ),
                    "season_year": 2027,
                }
            )
            canonical = CanonicalCaseQueryV1(
                mode=projection.mode,
                requested_parameter_keys=projection.parameter_keys,
                window_descriptors=(shifted.canonical_descriptor(),),
                normalized_intent_tokens=("analyze_current_state",),
            )
            query = query.model_copy(
                update={
                    "intent": query.intent.model_copy(
                        update={"window_ids": (shifted.window_id,)}
                    ),
                    "query_windows": (shifted,),
                    "canonical_query": canonical.canonical_text,
                    "canonical_query_digest": canonical.canonical_digest,
                }
            )

            assert await repository.search_candidates(query) == ()
        finally:
            await engine.dispose()

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_postgres_stable_tie_order_is_independent_of_reverse_insert_order() -> None:
    async def exercise(settings) -> None:
        engine = create_database_engine(settings)
        repository = PostgresRAGRepository(
            engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        try:
            sources = (
                await seed_completed_source(engine),
                await seed_completed_source(engine),
            )
            profile = embedding_profile()
            cutoff_item_id = str(max(item.result_item_id for item in sources))
            generation = await repository.create_building_generation(
                profile,
                sources[0].completed_at,
                cutoff_item_id,
            )
            for source in sources:
                await repository.enqueue(
                    pending_job(source, generation.generation_id)
                )
            jobs = await repository.claim(
                generation.generation_id,
                "stable-order-worker",
                datetime.now(UTC) + timedelta(minutes=1),
                2,
            )
            jobs_by_result = {item.source_result_item_id: item for item in jobs}
            projections = tuple(
                ready_projection(source, generation.generation_id, profile)
                for source in sources
            )
            for projection, fragments in reversed(projections):
                await repository.write_ready_projection(
                    jobs_by_result[projection.case_ref.result_item_id],
                    projection,
                    fragments,
                )
            active = await repository.activate_generation(
                generation.generation_id,
                datetime.now(UTC),
            )

            candidates = await repository.search_candidates(
                _query(
                    active,
                    projections[0][0],
                    HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW,
                )
            )

            assert tuple(item.case_ref.run_id for item in candidates) == tuple(
                sorted(item.case_ref.run_id for item in candidates)
            )
        finally:
            await engine.dispose()

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))
