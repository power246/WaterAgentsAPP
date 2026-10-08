"""Persist a bounded, typed Admin command for pending operation recovery.

Revision ID: 20261008_03
Revises: 20261008_02
"""

from alembic import op


revision = "20261008_03"
down_revision = "20261008_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE admin_operations DROP CONSTRAINT ck_admin_operations_summary")
    op.execute("""
        ALTER TABLE admin_operations ADD CONSTRAINT ck_admin_operations_summary
        CHECK (COALESCE((
          jsonb_typeof(safe_request_summary) = 'object'
          AND octet_length(safe_request_summary::text) <= 65536
          AND safe_request_summary ?& ARRAY['action', 'resource_identity']
          AND safe_request_summary - 'action' - 'resource_identity' - 'command' = '{}'::jsonb
          AND safe_request_summary->>'action' = kind
          AND safe_request_summary->>'resource_identity' = resource_identity
          AND (NOT safe_request_summary ? 'command' OR (
            jsonb_typeof(safe_request_summary->'command') = 'object'
            AND (safe_request_summary->'command') ?& ARRAY[
              'kind', 'resource_identity', 'facts_digest', 'requested_run_ids',
              'all_history', 'expected_delete_run_ids', 'expected_dependency_ids',
              'cascade_consumers', 'generation_id', 'safe_error_codes',
              'limit', 'desired_profile_id']
            AND (safe_request_summary->'command')
              - 'kind' - 'resource_identity' - 'facts_digest'
              - 'requested_run_ids' - 'all_history' - 'expected_delete_run_ids'
              - 'expected_dependency_ids' - 'cascade_consumers' - 'generation_id'
              - 'safe_error_codes' - 'limit' - 'desired_profile_id' = '{}'::jsonb
            AND safe_request_summary->'command'->>'kind' = kind
            AND safe_request_summary->'command'->>'resource_identity' = resource_identity
            AND safe_request_summary->'command'->>'facts_digest' ~ '^[0-9a-f]{64}$'
            AND jsonb_typeof(safe_request_summary->'command'->'requested_run_ids') = 'array'
            AND jsonb_typeof(safe_request_summary->'command'->'expected_delete_run_ids') = 'array'
            AND jsonb_typeof(safe_request_summary->'command'->'expected_dependency_ids') = 'array'
            AND jsonb_typeof(safe_request_summary->'command'->'safe_error_codes') = 'array'
            AND jsonb_typeof(safe_request_summary->'command'->'all_history') = 'boolean'
            AND jsonb_typeof(safe_request_summary->'command'->'cascade_consumers') = 'boolean'
          ))
        ), false))
    """)
    op.execute("ALTER TABLE admin_operations DROP CONSTRAINT ck_admin_operations_safe_result")
    _safe_result_constraint(32768)


def downgrade() -> None:
    op.execute("ALTER TABLE admin_operations DROP CONSTRAINT ck_admin_operations_safe_result")
    _safe_result_constraint(4096)
    op.execute("ALTER TABLE admin_operations DROP CONSTRAINT ck_admin_operations_summary")
    op.execute("""
        ALTER TABLE admin_operations ADD CONSTRAINT ck_admin_operations_summary
        CHECK (COALESCE((
          jsonb_typeof(safe_request_summary) = 'object'
          AND octet_length(safe_request_summary::text) <= 1024
          AND safe_request_summary ?& ARRAY['action', 'resource_identity']
          AND safe_request_summary - 'action' - 'resource_identity' = '{}'::jsonb
          AND safe_request_summary->>'action' = kind
          AND safe_request_summary->>'resource_identity' = resource_identity
        ), false))
    """)


def _safe_result_constraint(max_bytes: int) -> None:
    op.execute(f"""
        ALTER TABLE admin_operations ADD CONSTRAINT ck_admin_operations_safe_result
        CHECK (safe_result IS NULL OR COALESCE((
          jsonb_typeof(safe_result) = 'object'
          AND octet_length(safe_result::text) <= {max_bytes}
          AND safe_result->>'kind' = kind
          AND CASE kind
            WHEN 'history_cleanup' THEN
              safe_result ?& ARRAY['kind', 'deleted_run_ids', 'retained_idempotency_tombstones']
              AND safe_result - 'kind' - 'deleted_run_ids'
                  - 'retained_idempotency_tombstones' = '{{}}'::jsonb
              AND jsonb_typeof(safe_result->'deleted_run_ids') = 'array'
              AND NOT jsonb_path_exists(safe_result,
                '$.deleted_run_ids[*] ? (@.type() != "string")')
              AND jsonb_typeof(safe_result->'retained_idempotency_tombstones') = 'number'
              AND (safe_result->>'retained_idempotency_tombstones') ~ '^(0|[1-9][0-9]*)$'
            WHEN 'profile_handoff' THEN
              safe_result ?& ARRAY['kind', 'active_generation_id']
              AND safe_result - 'kind' - 'active_generation_id' = '{{}}'::jsonb
              AND jsonb_typeof(safe_result->'active_generation_id') = 'string'
              AND length(safe_result->>'active_generation_id') BETWEEN 1 AND 256
            WHEN 'retry_failed_jobs' THEN
              safe_result ?& ARRAY['kind', 'retried_jobs']
              AND safe_result - 'kind' - 'retried_jobs' = '{{}}'::jsonb
              AND jsonb_typeof(safe_result->'retried_jobs') = 'number'
              AND (safe_result->>'retried_jobs') ~ '^(0|[1-9][0-9]*)$'
            WHEN 'generation_deletion' THEN
              safe_result ?& ARRAY['kind', 'deleted_generation_id']
              AND safe_result - 'kind' - 'deleted_generation_id' = '{{}}'::jsonb
              AND jsonb_typeof(safe_result->'deleted_generation_id') = 'string'
              AND length(safe_result->>'deleted_generation_id') BETWEEN 1 AND 256
            ELSE false END
        ), false))
    """)
