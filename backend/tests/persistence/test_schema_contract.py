from app.infrastructure.persistence.schema import metadata


def test_schema_has_core_transcript_and_m8_rag_tables() -> None:
    assert set(metadata.tables) == {
        "analysis_runs",
        "analysis_sessions",
        "transcript_items",
        "transcript_contents",
        "rag_index_generations",
        "rag_index_jobs",
        "rag_case_projections",
        "rag_case_windows",
        "rag_fragment_projections",
        "rag_consumption_links",
        "runtime_settings",
        "unit_policy_versions",
        "unit_policy_entries",
        "unit_policy_confirmations",
        "analysis_run_idempotency_tombstones",
        "admin_operations",
        "guideline_documents",
        "guideline_versions",
        "guideline_catalog_state",
        "guideline_catalog_entries",
        "guideline_index_generations",
        "guideline_embeddings",
        "guideline_relation_embeddings",
        "guideline_relation_links",
        "guideline_operations",
    }


def test_schema_columns_do_not_duplicate_config_results_or_full_content() -> None:
    assert set(metadata.tables["analysis_runs"].c.keys()) == {
        "run_id",
        "status",
        "created_at",
        "updated_at",
        "query",
        "idempotency_key",
        "request_fingerprint",
        "creation_receipt",
        "request_item_id",
        "created_item_id",
        "terminal_item_id",
    }
    assert set(metadata.tables["analysis_sessions"].c.keys()) == {
        "session_id",
        "run_id",
        "agent_role",
        "target_comid",
        "status",
        "created_at",
        "updated_at",
        "created_item_id",
        "terminal_item_id",
    }
    assert set(metadata.tables["transcript_contents"].c.keys()) == {
        "item_id",
        "body",
        "created_at",
    }


def test_schema_has_the_declared_query_and_rag_indexes() -> None:
    index_names = {
        index.name
        for table in metadata.tables.values()
        for index in table.indexes
        if index.name is not None
    }
    assert index_names == {
        "ix_analysis_runs_history",
        "ix_analysis_sessions_directory",
        "uq_analysis_sessions_parser_per_run",
        "uq_analysis_sessions_run_summarizer_per_run",
        "uq_analysis_sessions_waterbody_per_run_comid",
        "ix_transcript_items_session_transcript",
        "ix_rag_generations_profile",
        "uq_rag_generations_one_active",
        "uq_rag_generations_one_building",
        "ix_rag_jobs_claim",
        "ix_rag_cases_source",
        "ix_rag_cases_scope",
        "ix_rag_cases_parameters_gin",
        "ix_rag_cases_search_document_gin",
        "ix_rag_windows_case",
        "ix_rag_windows_time_range_gist",
        "ix_rag_windows_parameters_gin",
        "ix_rag_fragments_case",
        "ix_rag_fragments_lookup",
        "ix_rag_consumption_source",
        "ix_rag_consumption_consumer",
        "uq_admin_operations_one_active_resource",
        "ix_admin_operations_recent",
        "uq_guideline_override_per_root",
        "uq_guideline_generation_one_active",
        "uq_guideline_generation_one_building",
        "ix_guideline_operations_claim",
    }


def test_rag_vector_is_unconstrained_and_has_no_approximate_index() -> None:
    embedding = metadata.tables["rag_case_projections"].c.embedding
    assert getattr(embedding.type, "dim", None) is None

    index_names = {
        index.name or ""
        for table in metadata.tables.values()
        for index in table.indexes
    }
    assert not any("hnsw" in name or "ivfflat" in name for name in index_names)


def test_circular_lifecycle_references_are_deferred() -> None:
    references = {
        constraint.name: constraint
        for table_name in ("analysis_runs", "analysis_sessions")
        for constraint in metadata.tables[table_name].foreign_key_constraints
        if constraint.name in {
            "fk_runs_request_item",
            "fk_runs_created_item",
            "fk_runs_terminal_item",
            "fk_sessions_created_item",
            "fk_sessions_terminal_item",
        }
    }
    assert len(references) == 5
    assert all(constraint.deferrable for constraint in references.values())
    assert all(constraint.initially == "DEFERRED" for constraint in references.values())
