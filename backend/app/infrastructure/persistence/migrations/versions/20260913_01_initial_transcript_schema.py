"""Create the M3 transcript persistence schema.

Revision ID: 20260913_01
Revises: None
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260913_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analysis_runs",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("idempotency_key", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("request_fingerprint", sa.LargeBinary(), nullable=True),
        sa.Column("creation_receipt", postgresql.JSONB(), nullable=False),
        sa.Column("request_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("terminal_item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', "
            "'cancelled', 'completed_with_persistence_error')",
            name=op.f("ck_analysis_runs_status_allowed"),
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name=op.f("ck_analysis_runs_timestamp_order"),
        ),
        sa.CheckConstraint(
            "length(btrim(query)) > 0",
            name=op.f("ck_analysis_runs_query_nonempty"),
        ),
        sa.CheckConstraint(
            "((idempotency_key IS NULL AND request_fingerprint IS NULL) OR "
            "(idempotency_key IS NOT NULL AND request_fingerprint IS NOT NULL))",
            name=op.f("ck_analysis_runs_idempotency_pair"),
        ),
        sa.CheckConstraint(
            "request_fingerprint IS NULL OR octet_length(request_fingerprint) = 32",
            name=op.f("ck_analysis_runs_fingerprint_sha256"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(creation_receipt) = 'object'",
            name=op.f("ck_analysis_runs_creation_receipt_object"),
        ),
        sa.CheckConstraint(
            "((status IN ('completed', 'failed', 'cancelled', "
            "'completed_with_persistence_error') AND terminal_item_id IS NOT NULL) "
            "OR (status IN ('pending', 'running') AND terminal_item_id IS NULL))",
            name=op.f("ck_analysis_runs_terminal_item_matches_status"),
        ),
        sa.PrimaryKeyConstraint("run_id", name="pk_analysis_runs"),
        sa.UniqueConstraint("idempotency_key", name="uq_analysis_runs_idempotency_key"),
    )
    op.create_index(
        "ix_analysis_runs_history",
        "analysis_runs",
        [sa.text("created_at DESC"), sa.text("run_id DESC")],
    )

    op.create_table(
        "analysis_sessions",
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_role", sa.Text(), nullable=False),
        sa.Column("target_comid", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("terminal_item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "((agent_role = 'query_parser' AND target_comid IS NULL) OR "
            "(agent_role = 'waterbody' AND target_comid IS NOT NULL "
            "AND length(btrim(target_comid)) > 0))",
            name=op.f("ck_analysis_sessions_role_comid"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'cancelled')",
            name=op.f("ck_analysis_sessions_status_allowed"),
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name=op.f("ck_analysis_sessions_timestamp_order"),
        ),
        sa.CheckConstraint(
            "((status IN ('completed', 'failed', 'cancelled') "
            "AND terminal_item_id IS NOT NULL) OR "
            "(status IN ('pending', 'running') AND terminal_item_id IS NULL))",
            name=op.f("ck_analysis_sessions_terminal_item_matches_status"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["analysis_runs.run_id"],
            name="fk_analysis_sessions_run_id_analysis_runs",
        ),
        sa.PrimaryKeyConstraint("session_id", name="pk_analysis_sessions"),
        sa.UniqueConstraint(
            "run_id",
            "session_id",
            name="uq_sessions_run_session",
        ),
    )
    op.create_index(
        "ix_analysis_sessions_directory",
        "analysis_sessions",
        ["run_id", "created_at", "session_id"],
    )
    op.create_index(
        "uq_analysis_sessions_parser_per_run",
        "analysis_sessions",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("agent_role = 'query_parser'"),
    )
    op.create_index(
        "uq_analysis_sessions_waterbody_per_run_comid",
        "analysis_sessions",
        ["run_id", "target_comid"],
        unique=True,
        postgresql_where=sa.text("agent_role = 'waterbody'"),
    )

    op.create_table(
        "transcript_items",
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("transcript_sequence", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("content_schema_version", sa.Integer(), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column(
            "rag_eligible",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("inline_body", sa.Text(), nullable=True),
        sa.Column("preview_head", sa.Text(), nullable=True),
        sa.Column("preview_tail", sa.Text(), nullable=True),
        sa.Column("content_bytes", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "transcript_sequence > 0",
            name=op.f("ck_transcript_items_sequence_positive"),
        ),
        sa.CheckConstraint(
            "content_schema_version > 0",
            name=op.f("ck_transcript_items_schema_version_positive"),
        ),
        sa.CheckConstraint(
            "length(btrim(content_type)) > 0",
            name=op.f("ck_transcript_items_content_type_nonempty"),
        ),
        sa.CheckConstraint(
            "content_bytes >= 0",
            name=op.f("ck_transcript_items_content_bytes_nonnegative"),
        ),
        sa.CheckConstraint(
            "((inline_body IS NOT NULL AND preview_head IS NULL AND preview_tail IS NULL) "
            "OR (inline_body IS NULL AND preview_head IS NOT NULL "
            "AND preview_tail IS NOT NULL))",
            name=op.f("ck_transcript_items_inline_or_preview"),
        ),
        sa.CheckConstraint(
            "inline_body IS NULL OR octet_length(inline_body) = content_bytes",
            name=op.f("ck_transcript_items_inline_byte_count"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["analysis_runs.run_id"],
            name="fk_transcript_items_run_id_analysis_runs",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "session_id"],
            ["analysis_sessions.run_id", "analysis_sessions.session_id"],
            name="fk_items_run_session",
        ),
        sa.PrimaryKeyConstraint("item_id", name="pk_transcript_items"),
        sa.UniqueConstraint("run_id", "item_id", name="uq_items_run_item"),
        sa.UniqueConstraint(
            "run_id",
            "transcript_sequence",
            name="uq_items_run_sequence",
        ),
    )
    op.create_index(
        "ix_transcript_items_session_transcript",
        "transcript_items",
        ["run_id", "session_id", "transcript_sequence"],
    )

    op.create_table(
        "transcript_contents",
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["transcript_items.item_id"],
            name="fk_transcript_contents_item_id_transcript_items",
        ),
        sa.PrimaryKeyConstraint("item_id", name="pk_transcript_contents"),
    )

    op.create_foreign_key(
        "fk_runs_request_item",
        "analysis_runs",
        "transcript_items",
        ["run_id", "request_item_id"],
        ["run_id", "item_id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_runs_created_item",
        "analysis_runs",
        "transcript_items",
        ["run_id", "created_item_id"],
        ["run_id", "item_id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_runs_terminal_item",
        "analysis_runs",
        "transcript_items",
        ["run_id", "terminal_item_id"],
        ["run_id", "item_id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_sessions_created_item",
        "analysis_sessions",
        "transcript_items",
        ["run_id", "created_item_id"],
        ["run_id", "item_id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_sessions_terminal_item",
        "analysis_sessions",
        "transcript_items",
        ["run_id", "terminal_item_id"],
        ["run_id", "item_id"],
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    op.drop_constraint("fk_sessions_terminal_item", "analysis_sessions", type_="foreignkey")
    op.drop_constraint("fk_sessions_created_item", "analysis_sessions", type_="foreignkey")
    op.drop_constraint("fk_runs_terminal_item", "analysis_runs", type_="foreignkey")
    op.drop_constraint("fk_runs_created_item", "analysis_runs", type_="foreignkey")
    op.drop_constraint("fk_runs_request_item", "analysis_runs", type_="foreignkey")
    op.drop_table("transcript_contents")
    op.drop_index("ix_transcript_items_session_transcript", table_name="transcript_items")
    op.drop_table("transcript_items")
    op.drop_index(
        "uq_analysis_sessions_waterbody_per_run_comid",
        table_name="analysis_sessions",
    )
    op.drop_index("uq_analysis_sessions_parser_per_run", table_name="analysis_sessions")
    op.drop_index("ix_analysis_sessions_directory", table_name="analysis_sessions")
    op.drop_table("analysis_sessions")
    op.drop_index("ix_analysis_runs_history", table_name="analysis_runs")
    op.drop_table("analysis_runs")
