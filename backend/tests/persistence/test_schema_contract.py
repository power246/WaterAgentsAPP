from app.infrastructure.persistence.schema import metadata


def test_schema_has_only_the_four_core_business_tables() -> None:
    assert set(metadata.tables) == {
        "analysis_runs",
        "analysis_sessions",
        "transcript_items",
        "transcript_contents",
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


def test_schema_has_only_the_current_query_indexes() -> None:
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
    }


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
