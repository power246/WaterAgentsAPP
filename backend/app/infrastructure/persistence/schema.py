from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

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
        "((agent_role = 'query_parser' AND target_comid IS NULL) OR "
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
    "ix_transcript_items_session_transcript",
    transcript_items.c.run_id,
    transcript_items.c.session_id,
    transcript_items.c.transcript_sequence,
)
