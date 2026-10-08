import asyncio

from alembic import command
from sqlalchemy import inspect, text

from app.infrastructure.persistence.database import (
    alembic_config,
    check_database_schema,
    create_database_engine,
    upgrade_database,
)
from scripts.test_database import temporary_migrated_schema


RAG_TABLES = {
    "rag_index_generations",
    "rag_index_jobs",
    "rag_case_projections",
    "rag_case_windows",
    "rag_fragment_projections",
    "rag_consumption_links",
}


def test_fresh_schema_has_vector_extension_tables_constraints_and_indexes() -> None:
    async def exercise(settings) -> None:
        engine = create_database_engine(settings)
        try:
            async with engine.connect() as connection:
                tables = set(
                    await connection.run_sync(
                        lambda sync_connection: inspect(sync_connection).get_table_names()
                    )
                )
                vector_version = await connection.scalar(
                    text(
                        "SELECT extversion FROM pg_extension "
                        "WHERE extname = 'vector'"
                    )
                )
                vector_type = await connection.scalar(
                    text(
                        "SELECT format_type(a.atttypid, a.atttypmod) "
                        "FROM pg_attribute a "
                        "JOIN pg_class c ON c.oid = a.attrelid "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = current_schema() "
                        "AND c.relname = 'rag_case_projections' "
                        "AND a.attname = 'embedding'"
                    )
                )
                constraint_definitions = tuple(
                    (
                        await connection.execute(
                            text(
                                "SELECT conname, pg_get_constraintdef(oid) AS definition "
                                "FROM pg_constraint "
                                "WHERE connamespace = current_schema()::regnamespace "
                                "AND conrelid::regclass::text LIKE 'rag_%' "
                                "ORDER BY conname"
                            )
                        )
                    ).mappings()
                )
                index_definitions = tuple(
                    (
                        await connection.execute(
                            text(
                                "SELECT indexname, indexdef FROM pg_indexes "
                                "WHERE schemaname = current_schema() "
                                "AND tablename LIKE 'rag_%' ORDER BY indexname"
                            )
                        )
                    ).mappings()
                )
        finally:
            await engine.dispose()

        constraints = {
            row["conname"]: row["definition"] for row in constraint_definitions
        }
        indexes = {row["indexname"]: row["indexdef"] for row in index_definitions}
        assert RAG_TABLES <= tables
        assert vector_version is not None
        assert tuple(int(part) for part in vector_version.split(".")) >= (0, 8, 0)
        assert vector_type in {"vector", "public.vector"}
        assert "uq_rag_jobs_identity" in constraints
        assert "fk_rag_cases_generation_profile" in constraints
        assert "fk_rag_fragments_source_item" in constraints
        assert "fk_rag_consumption_source_item" in constraints
        assert "transcript_items" in constraints["fk_rag_consumption_source_item"]
        assert "ON DELETE RESTRICT" in constraints["fk_rag_consumption_source_item"]
        assert all(
            name != "fk_rag_consumption_source_case" for name in constraints
        )
        assert "parameter_key" in constraints["pk_rag_fragment_projections"]
        assert "window_id" in constraints["pk_rag_fragment_projections"]
        assert "uq_rag_generations_one_active" in indexes
        assert "USING gin" in indexes["ix_rag_cases_search_document_gin"]
        assert "USING gist" in indexes["ix_rag_windows_time_range_gist"]
        assert not any(
            "hnsw" in definition.casefold() or "ivfflat" in definition.casefold()
            for definition in indexes.values()
        )

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_existing_four_table_schema_downgrade_upgrade_round_trip() -> None:
    async def table_names(settings) -> set[str]:
        engine = create_database_engine(settings)
        try:
            async with engine.connect() as connection:
                return set(
                    await connection.run_sync(
                        lambda sync_connection: inspect(sync_connection).get_table_names()
                    )
                )
        finally:
            await engine.dispose()

    with temporary_migrated_schema() as settings:
        config = alembic_config(settings)
        command.downgrade(config, "20261003_01")
        downgraded = asyncio.run(table_names(settings))
        assert RAG_TABLES.isdisjoint(downgraded)
        assert {
            "analysis_runs",
            "analysis_sessions",
            "transcript_items",
            "transcript_contents",
        } <= downgraded

        assert upgrade_database(settings) == "20261008_01"
        assert asyncio.run(check_database_schema(settings)) == "20261008_01"
        assert RAG_TABLES <= asyncio.run(table_names(settings))
