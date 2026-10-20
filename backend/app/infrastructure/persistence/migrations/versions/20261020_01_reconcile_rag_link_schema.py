"""Reconcile RAG tables created before the M8 source-scope correction.

Revision ID: 20261020_01
Revises: 20261008_03
"""

from alembic import op
from sqlalchemy import UUID, Column, text

revision = "20261020_01"
down_revision = "20261008_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    columns = set(connection.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'rag_consumption_links'"
    )).scalars())
    source_columns = {"source_run_id", "source_session_id"}
    if columns.isdisjoint(source_columns):
        op.add_column("rag_consumption_links", Column("source_run_id", UUID(), nullable=True))
        op.add_column("rag_consumption_links", Column("source_session_id", UUID(), nullable=True))
        connection.execute(text("""
            UPDATE rag_consumption_links AS link
            SET source_run_id = source.source_run_id,
                source_session_id = source.source_session_id
            FROM rag_case_projections AS source
            WHERE source.generation_id = link.source_generation_id
              AND source.case_id = link.source_case_id
              AND source.source_result_item_id = link.source_result_item_id
        """))
        if connection.scalar(text(
            "SELECT count(*) FROM rag_consumption_links "
            "WHERE source_run_id IS NULL OR source_session_id IS NULL"
        )):
            raise RuntimeError("legacy RAG consumption link has no source case")
        op.alter_column("rag_consumption_links", "source_run_id", nullable=False)
        op.alter_column("rag_consumption_links", "source_session_id", nullable=False)
        op.create_foreign_key(
            "fk_rag_consumption_source_item", "rag_consumption_links", "transcript_items",
            ["source_run_id", "source_session_id", "source_result_item_id"],
            ["run_id", "session_id", "item_id"], ondelete="RESTRICT",
        )
        op.drop_constraint("fk_rag_consumption_source_case", "rag_consumption_links")
        op.drop_index("ix_rag_consumption_source", table_name="rag_consumption_links")
        op.create_index("ix_rag_consumption_source", "rag_consumption_links",
                        ["source_run_id", "source_result_item_id"])
    elif not source_columns <= columns:
        raise RuntimeError("RAG consumption source columns are only partially present")

    primary_key = connection.scalar(text("""
        SELECT pg_get_constraintdef(oid) FROM pg_constraint
        WHERE conrelid = to_regclass('rag_fragment_projections')
          AND conname = 'pk_rag_fragment_projections'
    """))
    if primary_key == "PRIMARY KEY (generation_id, case_id, source_fragment_item_id)":
        op.drop_constraint("pk_rag_fragment_projections", "rag_fragment_projections")
        op.create_primary_key("pk_rag_fragment_projections", "rag_fragment_projections", [
            "generation_id", "case_id", "source_fragment_item_id", "parameter_key", "window_id",
        ])
    elif primary_key != (
        "PRIMARY KEY (generation_id, case_id, source_fragment_item_id, parameter_key, window_id)"
    ):
        raise RuntimeError("RAG fragment primary key has an unexpected shape")


def downgrade() -> None:
    # The preceding revision already declares the reconciled structure.
    pass
