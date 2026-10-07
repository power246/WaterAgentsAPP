"""Add the generation-scoped memory RAG persistence schema.

Revision ID: 20261007_01
Revises: 20261003_01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261007_01"
down_revision: str | None = "20261003_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_available_extensions WHERE name = 'vector'
          ) THEN
            RAISE EXCEPTION
              'required PostgreSQL extension vector is not available; use a pgvector-enabled image'
              USING ERRCODE = 'feature_not_supported';
          END IF;
        END
        $$
        """
    )
    op.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
    op.create_unique_constraint(
        "uq_items_run_session_item",
        "transcript_items",
        ("run_id", "session_id", "item_id"),
    )

    op.execute(
        """
        CREATE TABLE rag_index_generations (
          generation_id uuid NOT NULL,
          profile_id text NOT NULL,
          provider text NOT NULL,
          model text NOT NULL,
          output_dimensions integer NOT NULL,
          max_input_tokens integer NOT NULL,
          distance_metric text NOT NULL,
          semantic_min_similarity double precision NOT NULL,
          canonical_query_schema_version integer NOT NULL,
          canonical_template_version integer NOT NULL,
          projection_schema_version integer NOT NULL,
          profile_digest text NOT NULL,
          status text NOT NULL,
          source_cutoff_created_at timestamptz NOT NULL,
          source_cutoff_item_id uuid NOT NULL,
          created_at timestamptz NOT NULL,
          activated_at timestamptz,
          retired_at timestamptz,
          failed_at timestamptz,
          last_safe_error_code text,
          CONSTRAINT pk_rag_index_generations PRIMARY KEY (generation_id),
          CONSTRAINT uq_rag_generations_profile_dimensions
            UNIQUE (generation_id, profile_id, output_dimensions),
          CONSTRAINT ck_rag_index_generations_status_allowed
            CHECK (status IN ('building', 'active', 'retired', 'failed')),
          CONSTRAINT ck_rag_index_generations_dimensions_positive
            CHECK (output_dimensions > 0),
          CONSTRAINT ck_rag_index_generations_max_input_tokens_positive
            CHECK (max_input_tokens > 0),
          CONSTRAINT ck_rag_index_generations_distance_cosine
            CHECK (distance_metric = 'cosine'),
          CONSTRAINT ck_rag_index_generations_semantic_similarity_range
            CHECK (semantic_min_similarity >= 0 AND semantic_min_similarity <= 1),
          CONSTRAINT ck_rag_index_generations_schema_versions_positive
            CHECK (canonical_query_schema_version > 0
              AND canonical_template_version > 0
              AND projection_schema_version > 0),
          CONSTRAINT ck_rag_index_generations_profile_identity
            CHECK (profile_id = profile_digest),
          CONSTRAINT ck_rag_index_generations_status_timestamps CHECK (
            (status = 'building' AND activated_at IS NULL AND retired_at IS NULL
              AND failed_at IS NULL AND last_safe_error_code IS NULL)
            OR (status = 'active' AND activated_at IS NOT NULL AND retired_at IS NULL
              AND failed_at IS NULL AND last_safe_error_code IS NULL)
            OR (status = 'retired' AND activated_at IS NOT NULL AND retired_at IS NOT NULL
              AND failed_at IS NULL AND last_safe_error_code IS NULL)
            OR (status = 'failed' AND activated_at IS NULL AND retired_at IS NULL
              AND failed_at IS NOT NULL AND length(btrim(last_safe_error_code)) > 0)
          )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_rag_generations_profile "
        "ON rag_index_generations (profile_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_rag_generations_one_active "
        "ON rag_index_generations (status) WHERE status = 'active'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_rag_generations_one_building "
        "ON rag_index_generations (status) WHERE status = 'building'"
    )

    op.execute(
        """
        CREATE TABLE rag_index_jobs (
          job_id uuid NOT NULL,
          generation_id uuid NOT NULL,
          source_run_id uuid NOT NULL,
          source_session_id uuid NOT NULL,
          source_result_item_id uuid NOT NULL,
          projection_schema_version integer NOT NULL,
          status text NOT NULL,
          attempts integer NOT NULL,
          next_attempt_at timestamptz,
          lease_owner text,
          lease_until timestamptz,
          last_safe_error_code text,
          created_at timestamptz NOT NULL,
          updated_at timestamptz NOT NULL,
          completed_at timestamptz,
          CONSTRAINT pk_rag_index_jobs PRIMARY KEY (job_id),
          CONSTRAINT fk_rag_index_jobs_generation_id_rag_index_generations
            FOREIGN KEY (generation_id) REFERENCES rag_index_generations (generation_id)
              ON DELETE CASCADE,
          CONSTRAINT fk_rag_jobs_source_item
            FOREIGN KEY (source_run_id, source_session_id, source_result_item_id)
              REFERENCES transcript_items (run_id, session_id, item_id) ON DELETE CASCADE,
          CONSTRAINT uq_rag_jobs_identity
            UNIQUE (generation_id, source_result_item_id, projection_schema_version),
          CONSTRAINT ck_rag_index_jobs_status_allowed
            CHECK (status IN ('pending', 'leased', 'retry_wait', 'completed', 'excluded', 'failed')),
          CONSTRAINT ck_rag_index_jobs_schema_version_positive
            CHECK (projection_schema_version > 0),
          CONSTRAINT ck_rag_index_jobs_attempts_range
            CHECK (attempts >= 0 AND attempts <= 5),
          CONSTRAINT ck_rag_index_jobs_timestamp_order CHECK (updated_at >= created_at),
          CONSTRAINT ck_rag_index_jobs_status_fields CHECK (
            (status = 'leased' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL
              AND next_attempt_at IS NULL AND completed_at IS NULL)
            OR (status = 'retry_wait' AND lease_owner IS NULL AND lease_until IS NULL
              AND next_attempt_at IS NOT NULL AND last_safe_error_code IS NOT NULL
              AND completed_at IS NULL)
            OR (status = 'pending' AND lease_owner IS NULL AND lease_until IS NULL
              AND next_attempt_at IS NULL AND completed_at IS NULL)
            OR (status = 'completed' AND lease_owner IS NULL AND lease_until IS NULL
              AND next_attempt_at IS NULL AND completed_at IS NOT NULL
              AND last_safe_error_code IS NULL)
            OR (status IN ('excluded', 'failed') AND lease_owner IS NULL
              AND lease_until IS NULL AND next_attempt_at IS NULL
              AND completed_at IS NOT NULL AND last_safe_error_code IS NOT NULL)
          )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_rag_jobs_claim ON rag_index_jobs "
        "(generation_id, status, next_attempt_at, lease_until, job_id)"
    )

    op.execute(
        """
        CREATE TABLE rag_case_projections (
          generation_id uuid NOT NULL,
          case_id uuid NOT NULL,
          profile_id text NOT NULL,
          embedding_dimensions integer NOT NULL,
          source_run_id uuid NOT NULL,
          source_session_id uuid NOT NULL,
          source_result_item_id uuid NOT NULL,
          analysis_context_item_id uuid NOT NULL,
          target_comid text NOT NULL,
          projection_schema_version integer NOT NULL,
          source_content_type text NOT NULL,
          source_content_schema_version integer NOT NULL,
          mode text NOT NULL,
          parameter_keys text[] NOT NULL,
          window_ids text[] NOT NULL,
          source_completed_at timestamptz NOT NULL,
          canonical_query text NOT NULL,
          canonical_query_digest text NOT NULL,
          search_document tsvector NOT NULL DEFAULT ''::tsvector,
          embedding public.vector NOT NULL,
          result_projection jsonb NOT NULL,
          projection_digest text NOT NULL,
          ready_at timestamptz NOT NULL,
          CONSTRAINT pk_rag_case_projections PRIMARY KEY (generation_id, case_id),
          CONSTRAINT fk_rag_cases_generation_profile
            FOREIGN KEY (generation_id, profile_id, embedding_dimensions)
              REFERENCES rag_index_generations
                (generation_id, profile_id, output_dimensions) ON DELETE CASCADE,
          CONSTRAINT fk_rag_cases_source_result
            FOREIGN KEY (source_run_id, source_session_id, source_result_item_id)
              REFERENCES transcript_items (run_id, session_id, item_id) ON DELETE CASCADE,
          CONSTRAINT fk_rag_cases_analysis_context
            FOREIGN KEY (source_run_id, analysis_context_item_id)
              REFERENCES transcript_items (run_id, item_id) ON DELETE CASCADE,
          CONSTRAINT uq_rag_cases_result_identity
            UNIQUE (generation_id, case_id, source_result_item_id),
          CONSTRAINT uq_rag_cases_source_scope
            UNIQUE (generation_id, case_id, source_run_id, source_session_id,
              source_result_item_id),
          CONSTRAINT uq_rag_cases_generation_source
            UNIQUE (generation_id, source_result_item_id),
          CONSTRAINT ck_rag_case_projections_dimensions_positive
            CHECK (embedding_dimensions > 0),
          CONSTRAINT ck_rag_case_projections_embedding_dimensions_match
            CHECK (public.vector_dims(embedding) = embedding_dimensions),
          CONSTRAINT ck_rag_case_projections_parameters_nonempty
            CHECK (cardinality(parameter_keys) > 0),
          CONSTRAINT ck_rag_case_projections_windows_nonempty
            CHECK (cardinality(window_ids) > 0),
          CONSTRAINT ck_rag_case_projections_target_comid_nonempty
            CHECK (length(btrim(target_comid)) > 0),
          CONSTRAINT ck_rag_case_projections_canonical_query_nonempty
            CHECK (length(btrim(canonical_query)) > 0),
          CONSTRAINT ck_rag_case_projections_result_object
            CHECK (jsonb_typeof(result_projection) = 'object')
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_rag_cases_source ON rag_case_projections "
        "(source_run_id, source_session_id, source_result_item_id)"
    )
    op.execute(
        "CREATE INDEX ix_rag_cases_scope ON rag_case_projections "
        "(generation_id, target_comid, mode)"
    )
    op.execute(
        "CREATE INDEX ix_rag_cases_parameters_gin ON rag_case_projections "
        "USING gin (parameter_keys)"
    )
    op.execute(
        "CREATE INDEX ix_rag_cases_search_document_gin ON rag_case_projections "
        "USING gin (search_document)"
    )

    op.execute(
        """
        CREATE TABLE rag_case_windows (
          generation_id uuid NOT NULL,
          case_id uuid NOT NULL,
          window_id text NOT NULL,
          time_range tstzrange NOT NULL,
          local_months integer[] NOT NULL,
          season_bucket text,
          season_year integer,
          duration_seconds bigint NOT NULL,
          resolution text,
          parameter_keys text[] NOT NULL,
          CONSTRAINT pk_rag_case_windows PRIMARY KEY (generation_id, case_id, window_id),
          CONSTRAINT fk_rag_windows_case
            FOREIGN KEY (generation_id, case_id)
              REFERENCES rag_case_projections (generation_id, case_id) ON DELETE CASCADE,
          CONSTRAINT ck_rag_case_windows_time_range_nonempty CHECK (NOT isempty(time_range)),
          CONSTRAINT ck_rag_case_windows_duration_positive CHECK (duration_seconds > 0),
          CONSTRAINT ck_rag_case_windows_parameters_nonempty
            CHECK (cardinality(parameter_keys) > 0),
          CONSTRAINT ck_rag_case_windows_local_months_allowed
            CHECK (local_months <@ ARRAY[1,2,3,4,5,6,7,8,9,10,11,12]::integer[]),
          CONSTRAINT ck_rag_case_windows_season_bucket_allowed
            CHECK (season_bucket IS NULL OR season_bucket IN ('DJF', 'MAM', 'JJA', 'SON')),
          CONSTRAINT ck_rag_case_windows_season_pair
            CHECK ((season_bucket IS NULL) = (season_year IS NULL)),
          CONSTRAINT ck_rag_case_windows_resolution_allowed
            CHECK (resolution IS NULL OR resolution IN ('6h', '12h', '24h'))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_rag_windows_case ON rag_case_windows "
        "(generation_id, case_id, window_id)"
    )
    op.execute(
        "CREATE INDEX ix_rag_windows_time_range_gist ON rag_case_windows "
        "USING gist (time_range)"
    )
    op.execute(
        "CREATE INDEX ix_rag_windows_parameters_gin ON rag_case_windows "
        "USING gin (parameter_keys)"
    )

    op.execute(
        """
        CREATE TABLE rag_fragment_projections (
          generation_id uuid NOT NULL,
          case_id uuid NOT NULL,
          source_fragment_item_id uuid NOT NULL,
          source_run_id uuid NOT NULL,
          source_session_id uuid NOT NULL,
          evidence_id text NOT NULL,
          projection_schema_version integer NOT NULL,
          source_content_type text NOT NULL,
          source_content_schema_version integer NOT NULL,
          parameter_key text NOT NULL,
          window_id text NOT NULL,
          valid_time_range tstzrange NOT NULL,
          quality_tier text NOT NULL,
          applicability text NOT NULL,
          provenance_identity text NOT NULL,
          safe_projection jsonb NOT NULL,
          projection_digest text NOT NULL,
          CONSTRAINT pk_rag_fragment_projections
            PRIMARY KEY (generation_id, case_id, source_fragment_item_id),
          CONSTRAINT fk_rag_fragments_case
            FOREIGN KEY (generation_id, case_id)
              REFERENCES rag_case_projections (generation_id, case_id) ON DELETE CASCADE,
          CONSTRAINT fk_rag_fragments_window
            FOREIGN KEY (generation_id, case_id, window_id)
              REFERENCES rag_case_windows (generation_id, case_id, window_id)
                ON DELETE CASCADE,
          CONSTRAINT fk_rag_fragments_source_item
            FOREIGN KEY (source_run_id, source_session_id, source_fragment_item_id)
              REFERENCES transcript_items (run_id, session_id, item_id) ON DELETE CASCADE,
          CONSTRAINT ck_rag_fragment_projections_time_range_nonempty
            CHECK (NOT isempty(valid_time_range)),
          CONSTRAINT ck_rag_fragment_projections_projection_object
            CHECK (jsonb_typeof(safe_projection) = 'object')
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_rag_fragments_case ON rag_fragment_projections "
        "(generation_id, case_id, source_fragment_item_id)"
    )
    op.execute(
        "CREATE INDEX ix_rag_fragments_lookup ON rag_fragment_projections "
        "(generation_id, parameter_key, window_id)"
    )

    op.execute(
        """
        CREATE TABLE rag_consumption_links (
          link_id uuid NOT NULL,
          source_generation_id uuid NOT NULL,
          source_case_id uuid NOT NULL,
          source_result_item_id uuid NOT NULL,
          consumer_run_id uuid NOT NULL,
          consumer_session_id uuid NOT NULL,
          consumer_item_id uuid NOT NULL,
          created_at timestamptz NOT NULL,
          CONSTRAINT pk_rag_consumption_links PRIMARY KEY (link_id),
          CONSTRAINT fk_rag_consumption_source_case
            FOREIGN KEY (source_generation_id, source_case_id, source_result_item_id)
              REFERENCES rag_case_projections
                (generation_id, case_id, source_result_item_id) ON DELETE RESTRICT,
          CONSTRAINT fk_rag_consumption_consumer_item
            FOREIGN KEY (consumer_run_id, consumer_session_id, consumer_item_id)
              REFERENCES transcript_items (run_id, session_id, item_id) ON DELETE CASCADE,
          CONSTRAINT uq_rag_consumption_identity
            UNIQUE (source_generation_id, source_case_id, consumer_item_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_rag_consumption_source ON rag_consumption_links "
        "(source_generation_id, source_case_id)"
    )
    op.execute(
        "CREATE INDEX ix_rag_consumption_consumer ON rag_consumption_links "
        "(consumer_run_id, consumer_session_id, consumer_item_id)"
    )


def downgrade() -> None:
    op.drop_table("rag_consumption_links")
    op.drop_table("rag_fragment_projections")
    op.drop_table("rag_case_windows")
    op.drop_table("rag_case_projections")
    op.drop_table("rag_index_jobs")
    op.drop_table("rag_index_generations")
    op.drop_constraint(
        "uq_items_run_session_item",
        "transcript_items",
        type_="unique",
    )
