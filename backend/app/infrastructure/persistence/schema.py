from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.dialects.postgresql import JSONB, TSTZRANGE, TSVECTOR, UUID

metadata = MetaData(
    naming_convention={
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
    }
)

RUN_STATUSES = (
    "pending",
    "running",
    "completed",
    "failed",
    "cancelled",
    "completed_with_persistence_error",
)
RUN_TERMINAL_STATUSES = (
    "completed",
    "failed",
    "cancelled",
    "completed_with_persistence_error",
)
SESSION_STATUSES = ("pending", "running", "completed", "failed", "cancelled")
SESSION_TERMINAL_STATUSES = ("completed", "failed", "cancelled")


def _sql_values(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


analysis_runs = Table(
    "analysis_runs",
    metadata,
    Column("run_id", UUID(as_uuid=True), primary_key=True),
    Column("status", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("query", Text, nullable=False),
    Column("idempotency_key", UUID(as_uuid=True), nullable=True, unique=True),
    Column("request_fingerprint", LargeBinary, nullable=True),
    Column("creation_receipt", JSONB, nullable=False),
    Column("request_item_id", UUID(as_uuid=True), nullable=False),
    Column("created_item_id", UUID(as_uuid=True), nullable=False),
    Column("terminal_item_id", UUID(as_uuid=True), nullable=True),
    CheckConstraint(
        f"status IN ({_sql_values(RUN_STATUSES)})",
        name="status_allowed",
    ),
    CheckConstraint("updated_at >= created_at", name="timestamp_order"),
    CheckConstraint("length(btrim(query)) > 0", name="query_nonempty"),
    CheckConstraint(
        "((idempotency_key IS NULL AND request_fingerprint IS NULL) OR "
        "(idempotency_key IS NOT NULL AND request_fingerprint IS NOT NULL))",
        name="idempotency_pair",
    ),
    CheckConstraint(
        "request_fingerprint IS NULL OR octet_length(request_fingerprint) = 32",
        name="fingerprint_sha256",
    ),
    CheckConstraint(
        "jsonb_typeof(creation_receipt) = 'object'",
        name="creation_receipt_object",
    ),
    CheckConstraint(
        f"((status IN ({_sql_values(RUN_TERMINAL_STATUSES)}) "
        "AND terminal_item_id IS NOT NULL) OR "
        "(status IN ('pending', 'running') AND terminal_item_id IS NULL))",
        name="terminal_item_matches_status",
    ),
)

analysis_sessions = Table(
    "analysis_sessions",
    metadata,
    Column("session_id", UUID(as_uuid=True), primary_key=True),
    Column(
        "run_id",
        UUID(as_uuid=True),
        ForeignKey("analysis_runs.run_id"),
        nullable=False,
    ),
    Column("agent_role", Text, nullable=False),
    Column("target_comid", Text, nullable=True),
    Column("status", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("created_item_id", UUID(as_uuid=True), nullable=False),
    Column("terminal_item_id", UUID(as_uuid=True), nullable=True),
    UniqueConstraint("run_id", "session_id", name="uq_sessions_run_session"),
    CheckConstraint(
        "((agent_role IN ('query_parser', 'run_summarizer') "
        "AND target_comid IS NULL) OR "
        "(agent_role = 'waterbody' AND target_comid IS NOT NULL "
        "AND length(btrim(target_comid)) > 0))",
        name="role_comid",
    ),
    CheckConstraint(
        f"status IN ({_sql_values(SESSION_STATUSES)})",
        name="status_allowed",
    ),
    CheckConstraint("updated_at >= created_at", name="timestamp_order"),
    CheckConstraint(
        f"((status IN ({_sql_values(SESSION_TERMINAL_STATUSES)}) "
        "AND terminal_item_id IS NOT NULL) OR "
        "(status IN ('pending', 'running') AND terminal_item_id IS NULL))",
        name="terminal_item_matches_status",
    ),
)

transcript_items = Table(
    "transcript_items",
    metadata,
    Column("item_id", UUID(as_uuid=True), primary_key=True),
    Column(
        "run_id",
        UUID(as_uuid=True),
        ForeignKey("analysis_runs.run_id"),
        nullable=False,
    ),
    Column("session_id", UUID(as_uuid=True), nullable=True),
    Column("transcript_sequence", BigInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("content_type", Text, nullable=False),
    Column("content_schema_version", Integer, nullable=False),
    Column("request_id", UUID(as_uuid=True), nullable=True),
    Column("operation_id", UUID(as_uuid=True), nullable=True),
    Column("tags", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
    Column("rag_eligible", Boolean, nullable=False, server_default=text("false")),
    Column("inline_body", Text, nullable=True),
    Column("preview_head", Text, nullable=True),
    Column("preview_tail", Text, nullable=True),
    Column("content_bytes", BigInteger, nullable=False),
    UniqueConstraint("run_id", "item_id", name="uq_items_run_item"),
    UniqueConstraint(
        "run_id",
        "session_id",
        "item_id",
        name="uq_items_run_session_item",
    ),
    UniqueConstraint(
        "run_id",
        "transcript_sequence",
        name="uq_items_run_sequence",
    ),
    ForeignKeyConstraint(
        ("run_id", "session_id"),
        ("analysis_sessions.run_id", "analysis_sessions.session_id"),
        name="fk_items_run_session",
    ),
    CheckConstraint("transcript_sequence > 0", name="sequence_positive"),
    CheckConstraint("content_schema_version > 0", name="schema_version_positive"),
    CheckConstraint("length(btrim(content_type)) > 0", name="content_type_nonempty"),
    CheckConstraint("content_bytes >= 0", name="content_bytes_nonnegative"),
    CheckConstraint(
        "((inline_body IS NOT NULL AND preview_head IS NULL AND preview_tail IS NULL) "
        "OR (inline_body IS NULL AND preview_head IS NOT NULL "
        "AND preview_tail IS NOT NULL))",
        name="inline_or_preview",
    ),
    CheckConstraint(
        "inline_body IS NULL OR octet_length(inline_body) = content_bytes",
        name="inline_byte_count",
    ),
)

transcript_contents = Table(
    "transcript_contents",
    metadata,
    Column(
        "item_id",
        UUID(as_uuid=True),
        ForeignKey("transcript_items.item_id"),
        primary_key=True,
    ),
    Column("body", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

# These references are circular by design. Deferring them lets one transaction insert
# the root/session first and their immutable lifecycle transcript items second.
analysis_runs.append_constraint(
    ForeignKeyConstraint(
        ("run_id", "request_item_id"),
        ("transcript_items.run_id", "transcript_items.item_id"),
        name="fk_runs_request_item",
        deferrable=True,
        initially="DEFERRED",
        use_alter=True,
    )
)
analysis_runs.append_constraint(
    ForeignKeyConstraint(
        ("run_id", "created_item_id"),
        ("transcript_items.run_id", "transcript_items.item_id"),
        name="fk_runs_created_item",
        deferrable=True,
        initially="DEFERRED",
        use_alter=True,
    )
)
analysis_runs.append_constraint(
    ForeignKeyConstraint(
        ("run_id", "terminal_item_id"),
        ("transcript_items.run_id", "transcript_items.item_id"),
        name="fk_runs_terminal_item",
        deferrable=True,
        initially="DEFERRED",
        use_alter=True,
    )
)
analysis_sessions.append_constraint(
    ForeignKeyConstraint(
        ("run_id", "created_item_id"),
        ("transcript_items.run_id", "transcript_items.item_id"),
        name="fk_sessions_created_item",
        deferrable=True,
        initially="DEFERRED",
        use_alter=True,
    )
)
analysis_sessions.append_constraint(
    ForeignKeyConstraint(
        ("run_id", "terminal_item_id"),
        ("transcript_items.run_id", "transcript_items.item_id"),
        name="fk_sessions_terminal_item",
        deferrable=True,
        initially="DEFERRED",
        use_alter=True,
    )
)

Index(
    "ix_analysis_runs_history",
    analysis_runs.c.created_at.desc(),
    analysis_runs.c.run_id.desc(),
)
Index(
    "ix_analysis_sessions_directory",
    analysis_sessions.c.run_id,
    analysis_sessions.c.created_at,
    analysis_sessions.c.session_id,
)
Index(
    "uq_analysis_sessions_parser_per_run",
    analysis_sessions.c.run_id,
    unique=True,
    postgresql_where=analysis_sessions.c.agent_role == "query_parser",
)
Index(
    "uq_analysis_sessions_waterbody_per_run_comid",
    analysis_sessions.c.run_id,
    analysis_sessions.c.target_comid,
    unique=True,
    postgresql_where=analysis_sessions.c.agent_role == "waterbody",
)
Index(
    "uq_analysis_sessions_run_summarizer_per_run",
    analysis_sessions.c.run_id,
    unique=True,
    postgresql_where=analysis_sessions.c.agent_role == "run_summarizer",
)
Index(
    "ix_transcript_items_session_transcript",
    transcript_items.c.run_id,
    transcript_items.c.session_id,
    transcript_items.c.transcript_sequence,
)

RAG_GENERATION_STATUSES = ("building", "active", "retired", "failed")
RAG_JOB_STATUSES = (
    "pending",
    "leased",
    "retry_wait",
    "completed",
    "excluded",
    "failed",
)

rag_index_generations = Table(
    "rag_index_generations",
    metadata,
    Column("generation_id", UUID(as_uuid=True), primary_key=True),
    Column("profile_id", Text, nullable=False),
    Column("provider", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("output_dimensions", Integer, nullable=False),
    Column("max_input_tokens", Integer, nullable=False),
    Column("distance_metric", Text, nullable=False),
    Column("semantic_min_similarity", Float, nullable=False),
    Column("canonical_query_schema_version", Integer, nullable=False),
    Column("canonical_template_version", Integer, nullable=False),
    Column("projection_schema_version", Integer, nullable=False),
    Column("profile_digest", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("source_cutoff_created_at", DateTime(timezone=True), nullable=False),
    Column("source_cutoff_item_id", UUID(as_uuid=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("activated_at", DateTime(timezone=True), nullable=True),
    Column("retired_at", DateTime(timezone=True), nullable=True),
    Column("failed_at", DateTime(timezone=True), nullable=True),
    Column("last_safe_error_code", Text, nullable=True),
    UniqueConstraint(
        "generation_id",
        "profile_id",
        "output_dimensions",
        name="uq_rag_generations_profile_dimensions",
    ),
    CheckConstraint(
        f"status IN ({_sql_values(RAG_GENERATION_STATUSES)})",
        name="status_allowed",
    ),
    CheckConstraint("output_dimensions > 0", name="dimensions_positive"),
    CheckConstraint("max_input_tokens > 0", name="max_input_tokens_positive"),
    CheckConstraint("distance_metric = 'cosine'", name="distance_cosine"),
    CheckConstraint(
        "semantic_min_similarity >= 0 AND semantic_min_similarity <= 1",
        name="semantic_similarity_range",
    ),
    CheckConstraint(
        "canonical_query_schema_version > 0 AND "
        "canonical_template_version > 0 AND projection_schema_version > 0",
        name="schema_versions_positive",
    ),
    CheckConstraint("profile_id = profile_digest", name="profile_identity"),
    CheckConstraint(
        "((status = 'building' AND activated_at IS NULL AND retired_at IS NULL "
        "AND failed_at IS NULL AND last_safe_error_code IS NULL) OR "
        "(status = 'active' AND activated_at IS NOT NULL AND retired_at IS NULL "
        "AND failed_at IS NULL AND last_safe_error_code IS NULL) OR "
        "(status = 'retired' AND activated_at IS NOT NULL AND retired_at IS NOT NULL "
        "AND failed_at IS NULL AND last_safe_error_code IS NULL) OR "
        "(status = 'failed' AND activated_at IS NULL AND retired_at IS NULL "
        "AND failed_at IS NOT NULL AND length(btrim(last_safe_error_code)) > 0))",
        name="status_timestamps",
    ),
)

rag_index_jobs = Table(
    "rag_index_jobs",
    metadata,
    Column("job_id", UUID(as_uuid=True), primary_key=True),
    Column(
        "generation_id",
        UUID(as_uuid=True),
        ForeignKey("rag_index_generations.generation_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("source_run_id", UUID(as_uuid=True), nullable=False),
    Column("source_session_id", UUID(as_uuid=True), nullable=False),
    Column("source_result_item_id", UUID(as_uuid=True), nullable=False),
    Column("projection_schema_version", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("next_attempt_at", DateTime(timezone=True), nullable=True),
    Column("lease_owner", Text, nullable=True),
    Column("lease_until", DateTime(timezone=True), nullable=True),
    Column("last_safe_error_code", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    ForeignKeyConstraint(
        ("source_run_id", "source_session_id", "source_result_item_id"),
        ("transcript_items.run_id", "transcript_items.session_id", "transcript_items.item_id"),
        name="fk_rag_jobs_source_item",
        ondelete="CASCADE",
    ),
    UniqueConstraint(
        "generation_id",
        "source_result_item_id",
        "projection_schema_version",
        name="uq_rag_jobs_identity",
    ),
    CheckConstraint(
        f"status IN ({_sql_values(RAG_JOB_STATUSES)})",
        name="status_allowed",
    ),
    CheckConstraint("projection_schema_version > 0", name="schema_version_positive"),
    CheckConstraint("attempts >= 0 AND attempts <= 5", name="attempts_range"),
    CheckConstraint("updated_at >= created_at", name="timestamp_order"),
    CheckConstraint(
        "((status = 'leased' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL "
        "AND next_attempt_at IS NULL AND completed_at IS NULL) OR "
        "(status = 'retry_wait' AND lease_owner IS NULL AND lease_until IS NULL "
        "AND next_attempt_at IS NOT NULL AND last_safe_error_code IS NOT NULL "
        "AND completed_at IS NULL) OR "
        "(status IN ('pending') AND lease_owner IS NULL AND lease_until IS NULL "
        "AND next_attempt_at IS NULL AND completed_at IS NULL) OR "
        "(status = 'completed' AND lease_owner IS NULL AND lease_until IS NULL "
        "AND next_attempt_at IS NULL AND completed_at IS NOT NULL "
        "AND last_safe_error_code IS NULL) OR "
        "(status IN ('excluded', 'failed') AND lease_owner IS NULL "
        "AND lease_until IS NULL AND next_attempt_at IS NULL "
        "AND completed_at IS NOT NULL AND last_safe_error_code IS NOT NULL))",
        name="status_fields",
    ),
)

rag_case_projections = Table(
    "rag_case_projections",
    metadata,
    Column("generation_id", UUID(as_uuid=True), nullable=False),
    Column("case_id", UUID(as_uuid=True), nullable=False),
    Column("profile_id", Text, nullable=False),
    Column("embedding_dimensions", Integer, nullable=False),
    Column("source_run_id", UUID(as_uuid=True), nullable=False),
    Column("source_session_id", UUID(as_uuid=True), nullable=False),
    Column("source_result_item_id", UUID(as_uuid=True), nullable=False),
    Column("analysis_context_item_id", UUID(as_uuid=True), nullable=False),
    Column("target_comid", Text, nullable=False),
    Column("projection_schema_version", Integer, nullable=False),
    Column("source_content_type", Text, nullable=False),
    Column("source_content_schema_version", Integer, nullable=False),
    Column("mode", Text, nullable=False),
    Column("parameter_keys", ARRAY(Text), nullable=False),
    Column("window_ids", ARRAY(Text), nullable=False),
    Column("source_completed_at", DateTime(timezone=True), nullable=False),
    Column("canonical_query", Text, nullable=False),
    Column("canonical_query_digest", Text, nullable=False),
    Column(
        "search_document",
        TSVECTOR,
        nullable=False,
        server_default=text("''::tsvector"),
    ),
    Column("embedding", VECTOR(), nullable=False),
    Column("result_projection", JSONB, nullable=False),
    Column("projection_digest", Text, nullable=False),
    Column("ready_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ("generation_id", "profile_id", "embedding_dimensions"),
        (
            "rag_index_generations.generation_id",
            "rag_index_generations.profile_id",
            "rag_index_generations.output_dimensions",
        ),
        name="fk_rag_cases_generation_profile",
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ("source_run_id", "source_session_id", "source_result_item_id"),
        ("transcript_items.run_id", "transcript_items.session_id", "transcript_items.item_id"),
        name="fk_rag_cases_source_result",
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ("source_run_id", "analysis_context_item_id"),
        ("transcript_items.run_id", "transcript_items.item_id"),
        name="fk_rag_cases_analysis_context",
        ondelete="CASCADE",
    ),
    PrimaryKeyConstraint("generation_id", "case_id", name="pk_rag_case_projections"),
    UniqueConstraint(
        "generation_id",
        "case_id",
        "source_result_item_id",
        name="uq_rag_cases_result_identity",
    ),
    UniqueConstraint(
        "generation_id",
        "case_id",
        "source_run_id",
        "source_session_id",
        "source_result_item_id",
        name="uq_rag_cases_source_scope",
    ),
    UniqueConstraint(
        "generation_id",
        "source_result_item_id",
        name="uq_rag_cases_generation_source",
    ),
    CheckConstraint("embedding_dimensions > 0", name="dimensions_positive"),
    CheckConstraint(
        "vector_dims(embedding) = embedding_dimensions",
        name="embedding_dimensions_match",
    ),
    CheckConstraint("cardinality(parameter_keys) > 0", name="parameters_nonempty"),
    CheckConstraint("cardinality(window_ids) > 0", name="windows_nonempty"),
    CheckConstraint("length(btrim(target_comid)) > 0", name="target_comid_nonempty"),
    CheckConstraint("length(btrim(canonical_query)) > 0", name="canonical_query_nonempty"),
    CheckConstraint("jsonb_typeof(result_projection) = 'object'", name="result_object"),
)

rag_case_windows = Table(
    "rag_case_windows",
    metadata,
    Column("generation_id", UUID(as_uuid=True), nullable=False),
    Column("case_id", UUID(as_uuid=True), nullable=False),
    Column("window_id", Text, nullable=False),
    Column("time_range", TSTZRANGE, nullable=False),
    Column("local_months", ARRAY(Integer), nullable=False),
    Column("season_bucket", Text, nullable=True),
    Column("season_year", Integer, nullable=True),
    Column("duration_seconds", BigInteger, nullable=False),
    Column("resolution", Text, nullable=True),
    Column("parameter_keys", ARRAY(Text), nullable=False),
    ForeignKeyConstraint(
        ("generation_id", "case_id"),
        ("rag_case_projections.generation_id", "rag_case_projections.case_id"),
        name="fk_rag_windows_case",
        ondelete="CASCADE",
    ),
    PrimaryKeyConstraint(
        "generation_id", "case_id", "window_id", name="pk_rag_case_windows"
    ),
    CheckConstraint("NOT isempty(time_range)", name="time_range_nonempty"),
    CheckConstraint("duration_seconds > 0", name="duration_positive"),
    CheckConstraint("cardinality(parameter_keys) > 0", name="parameters_nonempty"),
    CheckConstraint(
        "local_months <@ ARRAY[1,2,3,4,5,6,7,8,9,10,11,12]::integer[]",
        name="local_months_allowed",
    ),
    CheckConstraint(
        "season_bucket IS NULL OR season_bucket IN ('DJF', 'MAM', 'JJA', 'SON')",
        name="season_bucket_allowed",
    ),
    CheckConstraint(
        "(season_bucket IS NULL) = (season_year IS NULL)",
        name="season_pair",
    ),
    CheckConstraint(
        "resolution IS NULL OR resolution IN ('6h', '12h', '24h')",
        name="resolution_allowed",
    ),
)

rag_fragment_projections = Table(
    "rag_fragment_projections",
    metadata,
    Column("generation_id", UUID(as_uuid=True), nullable=False),
    Column("case_id", UUID(as_uuid=True), nullable=False),
    Column("source_fragment_item_id", UUID(as_uuid=True), nullable=False),
    Column("source_run_id", UUID(as_uuid=True), nullable=False),
    Column("source_session_id", UUID(as_uuid=True), nullable=False),
    Column("evidence_id", Text, nullable=False),
    Column("projection_schema_version", Integer, nullable=False),
    Column("source_content_type", Text, nullable=False),
    Column("source_content_schema_version", Integer, nullable=False),
    Column("parameter_key", Text, nullable=False),
    Column("window_id", Text, nullable=False),
    Column("valid_time_range", TSTZRANGE, nullable=False),
    Column("quality_tier", Text, nullable=False),
    Column("applicability", Text, nullable=False),
    Column("provenance_identity", Text, nullable=False),
    Column("safe_projection", JSONB, nullable=False),
    Column("projection_digest", Text, nullable=False),
    ForeignKeyConstraint(
        ("generation_id", "case_id"),
        ("rag_case_projections.generation_id", "rag_case_projections.case_id"),
        name="fk_rag_fragments_case",
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ("generation_id", "case_id", "window_id"),
        (
            "rag_case_windows.generation_id",
            "rag_case_windows.case_id",
            "rag_case_windows.window_id",
        ),
        name="fk_rag_fragments_window",
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ("source_run_id", "source_session_id", "source_fragment_item_id"),
        ("transcript_items.run_id", "transcript_items.session_id", "transcript_items.item_id"),
        name="fk_rag_fragments_source_item",
        ondelete="CASCADE",
    ),
    PrimaryKeyConstraint(
        "generation_id",
        "case_id",
        "source_fragment_item_id",
        "parameter_key",
        "window_id",
        name="pk_rag_fragment_projections",
    ),
    CheckConstraint("NOT isempty(valid_time_range)", name="time_range_nonempty"),
    CheckConstraint("jsonb_typeof(safe_projection) = 'object'", name="projection_object"),
)

rag_consumption_links = Table(
    "rag_consumption_links",
    metadata,
    Column("link_id", UUID(as_uuid=True), primary_key=True),
    Column("source_generation_id", UUID(as_uuid=True), nullable=False),
    Column("source_case_id", UUID(as_uuid=True), nullable=False),
    Column("source_run_id", UUID(as_uuid=True), nullable=False),
    Column("source_session_id", UUID(as_uuid=True), nullable=False),
    Column("source_result_item_id", UUID(as_uuid=True), nullable=False),
    Column("consumer_run_id", UUID(as_uuid=True), nullable=False),
    Column("consumer_session_id", UUID(as_uuid=True), nullable=False),
    Column("consumer_item_id", UUID(as_uuid=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ("source_run_id", "source_session_id", "source_result_item_id"),
        ("transcript_items.run_id", "transcript_items.session_id", "transcript_items.item_id"),
        name="fk_rag_consumption_source_item",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ("consumer_run_id", "consumer_session_id", "consumer_item_id"),
        ("transcript_items.run_id", "transcript_items.session_id", "transcript_items.item_id"),
        name="fk_rag_consumption_consumer_item",
        ondelete="CASCADE",
    ),
    UniqueConstraint(
        "source_generation_id",
        "source_case_id",
        "consumer_item_id",
        name="uq_rag_consumption_identity",
    ),
)

Index("ix_rag_generations_profile", rag_index_generations.c.profile_id)
Index(
    "uq_rag_generations_one_active",
    rag_index_generations.c.status,
    unique=True,
    postgresql_where=rag_index_generations.c.status == "active",
)
Index(
    "uq_rag_generations_one_building",
    rag_index_generations.c.status,
    unique=True,
    postgresql_where=rag_index_generations.c.status == "building",
)
Index(
    "ix_rag_jobs_claim",
    rag_index_jobs.c.generation_id,
    rag_index_jobs.c.status,
    rag_index_jobs.c.next_attempt_at,
    rag_index_jobs.c.lease_until,
    rag_index_jobs.c.job_id,
)
Index(
    "ix_rag_cases_source",
    rag_case_projections.c.source_run_id,
    rag_case_projections.c.source_session_id,
    rag_case_projections.c.source_result_item_id,
)
Index(
    "ix_rag_cases_scope",
    rag_case_projections.c.generation_id,
    rag_case_projections.c.target_comid,
    rag_case_projections.c.mode,
)
Index(
    "ix_rag_cases_parameters_gin",
    rag_case_projections.c.parameter_keys,
    postgresql_using="gin",
)
Index(
    "ix_rag_cases_search_document_gin",
    rag_case_projections.c.search_document,
    postgresql_using="gin",
)
Index(
    "ix_rag_windows_case",
    rag_case_windows.c.generation_id,
    rag_case_windows.c.case_id,
    rag_case_windows.c.window_id,
)
Index(
    "ix_rag_windows_time_range_gist",
    rag_case_windows.c.time_range,
    postgresql_using="gist",
)
Index(
    "ix_rag_windows_parameters_gin",
    rag_case_windows.c.parameter_keys,
    postgresql_using="gin",
)
Index(
    "ix_rag_fragments_case",
    rag_fragment_projections.c.generation_id,
    rag_fragment_projections.c.case_id,
    rag_fragment_projections.c.source_fragment_item_id,
)
Index(
    "ix_rag_fragments_lookup",
    rag_fragment_projections.c.generation_id,
    rag_fragment_projections.c.parameter_key,
    rag_fragment_projections.c.window_id,
)
Index(
    "ix_rag_consumption_source",
    rag_consumption_links.c.source_run_id,
    rag_consumption_links.c.source_result_item_id,
)
Index(
    "ix_rag_consumption_consumer",
    rag_consumption_links.c.consumer_run_id,
    rag_consumption_links.c.consumer_session_id,
    rag_consumption_links.c.consumer_item_id,
)
