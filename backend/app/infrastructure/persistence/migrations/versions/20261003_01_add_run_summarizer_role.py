"""Allow one run summarizer session per analysis run.

Revision ID: 20261003_01
Revises: 20260913_01
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261003_01"
down_revision: str | None = "20260913_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_analysis_sessions_role_comid"),
        "analysis_sessions",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_analysis_sessions_role_comid"),
        "analysis_sessions",
        "((agent_role IN ('query_parser', 'run_summarizer') "
        "AND target_comid IS NULL) OR "
        "(agent_role = 'waterbody' AND target_comid IS NOT NULL "
        "AND length(btrim(target_comid)) > 0))",
    )
    op.create_index(
        "uq_analysis_sessions_run_summarizer_per_run",
        "analysis_sessions",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("agent_role = 'run_summarizer'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_analysis_sessions_run_summarizer_per_run",
        table_name="analysis_sessions",
    )
    op.drop_constraint(
        op.f("ck_analysis_sessions_role_comid"),
        "analysis_sessions",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_analysis_sessions_role_comid"),
        "analysis_sessions",
        "((agent_role = 'query_parser' AND target_comid IS NULL) OR "
        "(agent_role = 'waterbody' AND target_comid IS NOT NULL "
        "AND length(btrim(target_comid)) > 0))",
    )
