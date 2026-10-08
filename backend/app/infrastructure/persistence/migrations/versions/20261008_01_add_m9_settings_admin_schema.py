"""Persist M9 settings, unit policies, Admin operations and tombstones.

Revision ID: 20261008_01
Revises: 20261007_01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261008_01"
down_revision: str | None = "20261007_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BASELINE_REGISTRY_DIGEST = (
    "a8d311fbc1619acfbede2620f823ed4064d7fc8ee955878fefd202363b556644"
)


def upgrade() -> None:
    op.execute("""
        CREATE TABLE unit_policy_versions (
          version bigint PRIMARY KEY,
          registry_digest text NOT NULL,
          created_at timestamptz NOT NULL,
          sealed boolean NOT NULL DEFAULT false,
          CONSTRAINT ck_unit_policy_versions_version_positive CHECK (version > 0),
          CONSTRAINT ck_unit_policy_versions_digest_sha256
            CHECK (registry_digest ~ '^[0-9a-f]{64}$')
        )
    """)
    op.execute("""
        CREATE TABLE unit_policy_entries (
          version bigint NOT NULL REFERENCES unit_policy_versions(version)
            ON DELETE RESTRICT,
          parameter_code_system text NOT NULL,
          parameter_code text NOT NULL,
          target_unit_id text NOT NULL,
          CONSTRAINT pk_unit_policy_entries
            PRIMARY KEY (version, parameter_code_system, parameter_code),
          CONSTRAINT ck_unit_policy_entries_parameter
            CHECK (parameter_code_system = 'usgs'
              AND parameter_code ~ '^[0-9]{5}$'),
          CONSTRAINT ck_unit_policy_entries_target
            CHECK (length(btrim(target_unit_id)) BETWEEN 1 AND 100)
        )
    """)
    op.execute("""
        CREATE FUNCTION m9_guard_unit_policy_version() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'UPDATE' AND OLD.sealed = false AND NEW.sealed = true
             AND NEW.version = OLD.version
             AND NEW.registry_digest = OLD.registry_digest
             AND NEW.created_at = OLD.created_at THEN
            RETURN NEW;
          END IF;
          RAISE EXCEPTION 'sealed unit policy history is immutable'
            USING ERRCODE = 'check_violation';
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER tr_unit_policy_versions_append_only
        BEFORE UPDATE OR DELETE ON unit_policy_versions FOR EACH ROW
        EXECUTE FUNCTION m9_guard_unit_policy_version()
    """)
    op.execute("""
        CREATE FUNCTION m9_guard_unit_policy_entry() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'INSERT' AND EXISTS (
            SELECT 1 FROM unit_policy_versions
            WHERE version = NEW.version AND sealed = false
          ) THEN RETURN NEW; END IF;
          RAISE EXCEPTION 'sealed unit policy entries are immutable'
            USING ERRCODE = 'check_violation';
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER tr_unit_policy_entries_append_only
        BEFORE INSERT OR UPDATE OR DELETE ON unit_policy_entries FOR EACH ROW
        EXECUTE FUNCTION m9_guard_unit_policy_entry()
    """)

    # The digest is frozen in this revision. A changed runtime manifest must get a
    # reviewed successor migration rather than silently changing version 1.
    from app.infrastructure.config import load_parameter_capability_registry

    registry = load_parameter_capability_registry()
    if registry.manifest_digest != _BASELINE_REGISTRY_DIGEST:
        raise RuntimeError("M9 unit registry differs from the reviewed version 1 baseline")
    op.get_bind().execute(
        sa.text("""
            INSERT INTO unit_policy_versions (version, registry_digest, created_at)
            VALUES (1, :digest, now())
        """),
        {"digest": _BASELINE_REGISTRY_DIGEST},
    )
    op.get_bind().execute(
        sa.text("""
            INSERT INTO unit_policy_entries
              (version, parameter_code_system, parameter_code, target_unit_id)
            VALUES (1, :code_system, :code, :target_unit_id)
        """),
        [
            {
                "code_system": capability.key.code_system,
                "code": capability.key.code,
                "target_unit_id": capability.unit_policy_id,
            }
            for capability in registry.capabilities
        ],
    )
    op.execute("UPDATE unit_policy_versions SET sealed = true WHERE version = 1")

    op.execute("""
        CREATE TABLE runtime_settings (
          singleton_id integer PRIMARY KEY,
          schema_version integer NOT NULL,
          revision bigint NOT NULL CONSTRAINT uq_runtime_settings_revision UNIQUE,
          analysis_timezone text NOT NULL,
          parser_provider text NOT NULL,
          parser_model text NOT NULL,
          waterbody_provider text NOT NULL,
          waterbody_model text NOT NULL,
          memory_mode text NOT NULL,
          time_resolution text NOT NULL,
          desired_embedding_profile_id text NOT NULL,
          active_unit_policy_version bigint NOT NULL
            REFERENCES unit_policy_versions(version) ON DELETE RESTRICT,
          updated_at timestamptz NOT NULL,
          CONSTRAINT ck_runtime_settings_singleton CHECK (singleton_id = 1),
          CONSTRAINT ck_runtime_settings_schema_version CHECK (schema_version = 1),
          CONSTRAINT ck_runtime_settings_revision_positive CHECK (revision > 0),
          CONSTRAINT ck_runtime_settings_timezone
            CHECK (length(btrim(analysis_timezone)) BETWEEN 1 AND 100),
          CONSTRAINT ck_runtime_settings_models CHECK (
            length(btrim(parser_provider)) BETWEEN 1 AND 100 AND
            length(btrim(parser_model)) BETWEEN 1 AND 200 AND
            length(btrim(waterbody_provider)) BETWEEN 1 AND 100 AND
            length(btrim(waterbody_model)) BETWEEN 1 AND 200),
          CONSTRAINT ck_runtime_settings_memory_mode
            CHECK (memory_mode IN ('stateless', 'memory_augmented')),
          CONSTRAINT ck_runtime_settings_time_resolution
            CHECK (time_resolution IN ('auto', '6h', '12h', '24h')),
          CONSTRAINT ck_runtime_settings_profile_id
            CHECK (desired_embedding_profile_id ~ '^sha256:[0-9a-f]{64}$')
        )
    """)
    op.execute("""
        CREATE FUNCTION m9_require_sealed_policy() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM unit_policy_versions
                         WHERE version = NEW.active_unit_policy_version
                           AND sealed = true) THEN
            RAISE EXCEPTION 'active unit policy must be sealed'
              USING ERRCODE = 'check_violation';
          END IF;
          RETURN NEW;
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER tr_runtime_settings_sealed_policy
        BEFORE INSERT OR UPDATE OF active_unit_policy_version ON runtime_settings
        FOR EACH ROW EXECUTE FUNCTION m9_require_sealed_policy()
    """)
    op.execute("""
        CREATE TABLE analysis_run_idempotency_tombstones (
          idempotency_key uuid PRIMARY KEY,
          request_fingerprint bytea NOT NULL,
          deleted_run_id uuid NOT NULL
            CONSTRAINT uq_analysis_run_idempotency_tombstones_deleted_run_id UNIQUE,
          deleted_at timestamptz NOT NULL,
          CONSTRAINT ck_analysis_run_tombstones_fingerprint_sha256
            CHECK (octet_length(request_fingerprint) = 32),
          CONSTRAINT ck_analysis_run_tombstones_key_v4
            CHECK (substring(idempotency_key::text from 15 for 1) = '4')
        )
    """)
    op.execute("""
        CREATE FUNCTION m9_reject_live_tombstone_collision() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.idempotency_key IS NULL THEN RETURN NEW; END IF;
          PERFORM pg_advisory_xact_lock(hashtextextended(NEW.idempotency_key::text, 0));
          IF TG_TABLE_NAME = 'analysis_runs' THEN
            IF EXISTS (SELECT 1 FROM analysis_run_idempotency_tombstones
                       WHERE idempotency_key = NEW.idempotency_key) THEN
              RAISE EXCEPTION 'idempotency key was deleted'
                USING ERRCODE = 'unique_violation';
            END IF;
          ELSE
            IF EXISTS (SELECT 1 FROM analysis_runs
                       WHERE idempotency_key = NEW.idempotency_key) THEN
              RAISE EXCEPTION 'idempotency key is live'
                USING ERRCODE = 'unique_violation';
            END IF;
          END IF;
          RETURN NEW;
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER tr_analysis_runs_no_tombstone
        BEFORE INSERT OR UPDATE OF idempotency_key ON analysis_runs
        FOR EACH ROW EXECUTE FUNCTION m9_reject_live_tombstone_collision()
    """)
    op.execute("""
        CREATE TRIGGER tr_tombstones_no_live_run
        BEFORE INSERT ON analysis_run_idempotency_tombstones
        FOR EACH ROW EXECUTE FUNCTION m9_reject_live_tombstone_collision()
    """)

    op.execute("""
        CREATE TABLE admin_operations (
          operation_id uuid PRIMARY KEY,
          kind text NOT NULL,
          state text NOT NULL,
          resource_identity text NOT NULL,
          request_fingerprint bytea NOT NULL,
          idempotency_key uuid NOT NULL,
          confirmation_token_digest bytea NOT NULL,
          safe_request_summary jsonb NOT NULL,
          processed_items integer NOT NULL DEFAULT 0,
          total_items integer,
          succeeded_items integer NOT NULL DEFAULT 0,
          failed_items integer NOT NULL DEFAULT 0,
          safe_result jsonb,
          safe_error jsonb,
          created_at timestamptz NOT NULL,
          started_at timestamptz,
          finished_at timestamptz,
          CONSTRAINT ck_admin_operations_kind CHECK (kind IN
            ('history_cleanup', 'profile_handoff', 'retry_failed_jobs',
             'generation_deletion')),
          CONSTRAINT ck_admin_operations_state CHECK (state IN
            ('pending', 'running', 'succeeded', 'failed', 'interrupted')),
          CONSTRAINT ck_admin_operations_resource
            CHECK (length(btrim(resource_identity)) BETWEEN 1 AND 256),
          CONSTRAINT ck_admin_operations_fingerprints CHECK
            (octet_length(request_fingerprint) = 32 AND
             octet_length(confirmation_token_digest) = 32),
          CONSTRAINT ck_admin_operations_idempotency_v4
            CHECK (substring(idempotency_key::text from 15 for 1) = '4'),
          CONSTRAINT uq_admin_operations_idempotency_key UNIQUE (idempotency_key),
          CONSTRAINT uq_admin_operations_confirmation_digest
            UNIQUE (confirmation_token_digest),
          CONSTRAINT ck_admin_operations_progress CHECK (
            processed_items >= 0 AND succeeded_items >= 0 AND failed_items >= 0
            AND succeeded_items + failed_items <= processed_items
            AND (total_items IS NULL OR
              (total_items >= 0 AND processed_items <= total_items))),
          CONSTRAINT ck_admin_operations_summary CHECK (COALESCE((
            jsonb_typeof(safe_request_summary) = 'object'
            AND octet_length(safe_request_summary::text) <= 1024
            AND safe_request_summary ?& ARRAY['action', 'resource_identity']
            AND safe_request_summary - 'action' - 'resource_identity' = '{}'::jsonb
            AND safe_request_summary->>'action' = kind
            AND safe_request_summary->>'resource_identity' = resource_identity), false)),
          CONSTRAINT ck_admin_operations_safe_result CHECK (
            safe_result IS NULL OR COALESCE((
              jsonb_typeof(safe_result) = 'object'
              AND octet_length(safe_result::text) <= 4096
              AND safe_result->>'kind' = kind
              AND CASE kind
                WHEN 'history_cleanup' THEN
                  safe_result ?& ARRAY['kind', 'deleted_run_ids', 'retained_idempotency_tombstones']
                  AND
                  safe_result - 'kind' - 'deleted_run_ids' - 'retained_idempotency_tombstones' = '{}'::jsonb
                  AND jsonb_typeof(safe_result->'deleted_run_ids') = 'array'
                  AND NOT jsonb_path_exists(safe_result,
                    '$.deleted_run_ids[*] ? (@.type() != "string")')
                  AND jsonb_typeof(safe_result->'retained_idempotency_tombstones') = 'number'
                  AND (safe_result->>'retained_idempotency_tombstones') ~ '^(0|[1-9][0-9]*)$'
                WHEN 'profile_handoff' THEN
                  safe_result ?& ARRAY['kind', 'active_generation_id']
                  AND
                  safe_result - 'kind' - 'active_generation_id' = '{}'::jsonb
                  AND jsonb_typeof(safe_result->'active_generation_id') = 'string'
                  AND length(safe_result->>'active_generation_id') BETWEEN 1 AND 256
                WHEN 'retry_failed_jobs' THEN
                  safe_result ?& ARRAY['kind', 'retried_jobs']
                  AND
                  safe_result - 'kind' - 'retried_jobs' = '{}'::jsonb
                  AND jsonb_typeof(safe_result->'retried_jobs') = 'number'
                  AND (safe_result->>'retried_jobs') ~ '^(0|[1-9][0-9]*)$'
                WHEN 'generation_deletion' THEN
                  safe_result ?& ARRAY['kind', 'deleted_generation_id']
                  AND
                  safe_result - 'kind' - 'deleted_generation_id' = '{}'::jsonb
                  AND jsonb_typeof(safe_result->'deleted_generation_id') = 'string'
                  AND length(safe_result->>'deleted_generation_id') BETWEEN 1 AND 256
                ELSE false END), false)),
          CONSTRAINT ck_admin_operations_safe_error CHECK (
            safe_error IS NULL OR COALESCE((
              jsonb_typeof(safe_error) = 'object'
              AND octet_length(safe_error::text) <= 1024
              AND safe_error ?& ARRAY['code', 'message', 'retryable']
              AND safe_error - 'code' - 'message' - 'retryable' = '{}'::jsonb
              AND safe_error->>'code' ~ '^[a-z][a-z0-9_]*$'
              AND length(safe_error->>'message') BETWEEN 1 AND 256
              AND jsonb_typeof(safe_error->'retryable') = 'boolean'), false)),
          CONSTRAINT ck_admin_operations_lifecycle CHECK (
            (state = 'pending' AND started_at IS NULL AND finished_at IS NULL
              AND safe_result IS NULL AND safe_error IS NULL)
            OR (state = 'running' AND started_at IS NOT NULL AND finished_at IS NULL
              AND safe_result IS NULL AND safe_error IS NULL)
            OR (state = 'succeeded' AND started_at IS NOT NULL AND finished_at IS NOT NULL
              AND safe_result IS NOT NULL AND safe_error IS NULL)
            OR (state IN ('failed', 'interrupted') AND finished_at IS NOT NULL
              AND safe_result IS NULL AND safe_error IS NOT NULL)),
          CONSTRAINT ck_admin_operations_timestamps CHECK (
            (started_at IS NULL OR started_at >= created_at)
            AND (finished_at IS NULL OR finished_at >= coalesce(started_at, created_at)))
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_admin_operations_one_active_resource
        ON admin_operations (kind, resource_identity)
        WHERE state IN ('pending', 'running')
    """)
    op.execute("""
        CREATE INDEX ix_admin_operations_recent
        ON admin_operations (created_at DESC, operation_id DESC)
    """)
    op.execute("""
        CREATE FUNCTION m9_guard_admin_operation_transition() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.state IN ('succeeded', 'failed', 'interrupted') THEN
            RAISE EXCEPTION 'terminal Admin operation is immutable'
              USING ERRCODE = 'check_violation';
          END IF;
          IF NEW.operation_id != OLD.operation_id OR NEW.kind != OLD.kind
             OR NEW.resource_identity != OLD.resource_identity
             OR NEW.created_at != OLD.created_at
             OR NEW.idempotency_key != OLD.idempotency_key
             OR NEW.request_fingerprint != OLD.request_fingerprint
             OR NEW.confirmation_token_digest != OLD.confirmation_token_digest
             OR NEW.safe_request_summary != OLD.safe_request_summary THEN
            RAISE EXCEPTION 'Admin operation identity is immutable'
              USING ERRCODE = 'check_violation';
          END IF;
          IF NOT ((OLD.state = 'pending' AND NEW.state IN ('running', 'interrupted'))
             OR (OLD.state = 'running' AND NEW.state IN
                  ('running', 'succeeded', 'failed', 'interrupted'))) THEN
            RAISE EXCEPTION 'invalid Admin operation state transition'
              USING ERRCODE = 'check_violation';
          END IF;
          IF NEW.processed_items < OLD.processed_items
             OR NEW.succeeded_items < OLD.succeeded_items
             OR NEW.failed_items < OLD.failed_items THEN
            RAISE EXCEPTION 'Admin operation progress decreased'
              USING ERRCODE = 'check_violation';
          END IF;
          RETURN NEW;
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER tr_admin_operations_state
        BEFORE UPDATE ON admin_operations FOR EACH ROW
        EXECUTE FUNCTION m9_guard_admin_operation_transition()
    """)


def downgrade() -> None:
    op.execute("DROP TABLE admin_operations")
    op.execute("DROP FUNCTION m9_guard_admin_operation_transition()")
    op.execute("DROP TRIGGER tr_analysis_runs_no_tombstone ON analysis_runs")
    op.execute("DROP TABLE analysis_run_idempotency_tombstones")
    op.execute("DROP FUNCTION m9_reject_live_tombstone_collision()")
    op.execute("DROP TABLE runtime_settings")
    op.execute("DROP FUNCTION m9_require_sealed_policy()")
    op.execute("DROP TABLE unit_policy_entries")
    op.execute("DROP TABLE unit_policy_versions")
    op.execute("DROP FUNCTION m9_guard_unit_policy_entry()")
    op.execute("DROP FUNCTION m9_guard_unit_policy_version()")
