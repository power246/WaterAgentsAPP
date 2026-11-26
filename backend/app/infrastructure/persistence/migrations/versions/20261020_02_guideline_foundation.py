"""Guideline immutable content, catalog intervals and durable operations."""
from alembic import op

revision = "20261020_02"
down_revision = "20261020_01"
branch_labels = None
depends_on = None

# Frozen DDL; application startup never creates these objects.
_DDL = (
    """CREATE TABLE guideline_documents (
	document_id UUID NOT NULL,
	family_root_id UUID NOT NULL,
	identity JSONB NOT NULL,
	origin TEXT NOT NULL,
	bundle_key TEXT,
	based_on_version_id UUID,
	base_content_digest TEXT,
	draft JSONB,
	draft_digest TEXT,
	revision BIGINT NOT NULL,
	activation_epoch BIGINT DEFAULT '0' NOT NULL,
	disabled BOOLEAN DEFAULT 'false' NOT NULL,
	active_version_id UUID,
	pending_version_id UUID,
	CONSTRAINT pk_guideline_documents PRIMARY KEY (document_id),
	CONSTRAINT uq_guideline_documents_document_id UNIQUE (document_id, family_root_id),
	CONSTRAINT uq_guideline_documents_bundle_key UNIQUE (bundle_key),
	CONSTRAINT ck_guideline_documents_revisions CHECK (revision >= 0 AND activation_epoch >= 0),
	CONSTRAINT ck_guideline_documents_draft_pair CHECK ((draft IS NULL) = (draft_digest IS NULL)),
	CONSTRAINT ck_guideline_documents_origin CHECK (origin IN ('bundled', 'local_custom', 'local_override')),
	CONSTRAINT ck_guideline_documents_family_identity CHECK ((origin = 'local_override' AND document_id <> family_root_id AND based_on_version_id IS NOT NULL AND base_content_digest IS NOT NULL AND bundle_key IS NULL) OR (origin <> 'local_override' AND document_id = family_root_id AND based_on_version_id IS NULL AND base_content_digest IS NULL)),
	CONSTRAINT ck_guideline_documents_bundle_identity CHECK ((origin = 'bundled') = (bundle_key IS NOT NULL)),
	CONSTRAINT ck_guideline_documents_bundled_readonly CHECK (origin <> 'bundled' OR draft IS NULL),
	CONSTRAINT fk_guideline_documents_family_root_id_guideline_documents FOREIGN KEY(family_root_id) REFERENCES guideline_documents (document_id)
)""",
    """CREATE TABLE guideline_index_generations (
	generation_id UUID NOT NULL,
	profile JSONB NOT NULL,
	profile_id TEXT NOT NULL,
	profile_digest TEXT NOT NULL,
	dimensions INTEGER NOT NULL,
	state TEXT NOT NULL,
	catalog_revision BIGINT NOT NULL,
	CONSTRAINT pk_guideline_index_generations PRIMARY KEY (generation_id),
	CONSTRAINT uq_guideline_index_generations_generation_id UNIQUE (generation_id, profile_id, dimensions),
	CONSTRAINT ck_guideline_index_generations_state CHECK (state IN ('building', 'active', 'retired', 'failed')),
	CONSTRAINT ck_guideline_index_generations_bounds CHECK (dimensions > 0 AND catalog_revision >= 0),
	CONSTRAINT ck_guideline_index_generations_profile CHECK ((profile->>'namespace' = 'guideline' AND profile->>'profile_id' = profile_id AND profile->>'profile_digest' = profile_digest AND (profile->>'output_dimensions')::integer = dimensions) IS TRUE)
)""",
    """CREATE TABLE guideline_catalog_state (
	singleton_id SERIAL NOT NULL,
	revision BIGINT NOT NULL,
	active_generation_id UUID,
	CONSTRAINT pk_guideline_catalog_state PRIMARY KEY (singleton_id),
	CONSTRAINT ck_guideline_catalog_state_singleton CHECK (singleton_id = 1 AND revision >= 0),
	CONSTRAINT fk_guideline_catalog_state_active_generation_id_guideli_e6d6 FOREIGN KEY(active_generation_id) REFERENCES guideline_index_generations (generation_id)
)""",
    """CREATE TABLE guideline_versions (
	version_id UUID NOT NULL,
	document_id UUID NOT NULL,
	family_root_id UUID NOT NULL,
	version_number INTEGER NOT NULL,
	content_digest TEXT NOT NULL,
	snapshot JSONB NOT NULL,
	CONSTRAINT pk_guideline_versions PRIMARY KEY (version_id),
	CONSTRAINT uq_guideline_version_number UNIQUE (document_id, version_number),
	CONSTRAINT uq_guideline_version_owner UNIQUE (document_id, version_id),
	CONSTRAINT uq_guideline_version_digest UNIQUE (document_id, version_id, content_digest),
	CONSTRAINT uq_guideline_versions_family_root_id UNIQUE (family_root_id, version_id),
	CONSTRAINT ck_guideline_versions_number CHECK (version_number > 0),
	CONSTRAINT fk_guideline_versions_document_id_guideline_documents FOREIGN KEY(document_id, family_root_id) REFERENCES guideline_documents (document_id, family_root_id)
)""",
    """CREATE TABLE guideline_catalog_entries (
	family_root_id UUID NOT NULL,
	version_id UUID NOT NULL,
	valid_from_revision BIGINT NOT NULL,
	valid_to_revision BIGINT,
	CONSTRAINT pk_guideline_catalog_entries PRIMARY KEY (family_root_id, valid_from_revision),
	CONSTRAINT ck_guideline_catalog_entries_interval CHECK (valid_from_revision >= 0 AND (valid_to_revision IS NULL OR valid_to_revision > valid_from_revision)),
	CONSTRAINT fk_guideline_catalog_entries_family_root_id_guideline_versions FOREIGN KEY(family_root_id, version_id) REFERENCES guideline_versions (family_root_id, version_id),
	CONSTRAINT ex_guideline_catalog_no_overlap EXCLUDE USING gist (family_root_id public.gist_uuid_ops WITH =, int8range(valid_from_revision, valid_to_revision, '[)') WITH &&)
)""",
    """CREATE TABLE guideline_embeddings (
	generation_id UUID NOT NULL,
	version_id UUID NOT NULL,
	profile_id TEXT NOT NULL,
	dimensions INTEGER NOT NULL,
	canonical_document_digest TEXT NOT NULL,
	embedding public.vector NOT NULL,
	CONSTRAINT pk_guideline_embeddings PRIMARY KEY (generation_id, version_id),
	CONSTRAINT fk_guideline_embeddings_version_id_guideline_versions FOREIGN KEY(version_id) REFERENCES guideline_versions (version_id),
	CONSTRAINT fk_guideline_embeddings_generation_id_guideline_index_g_4289 FOREIGN KEY(generation_id, profile_id, dimensions) REFERENCES guideline_index_generations (generation_id, profile_id, dimensions),
	CONSTRAINT ck_guideline_embeddings_vector CHECK (public.vector_dims(embedding) = dimensions AND public.vector_norm(embedding) BETWEEN 1e-12 AND 1e6)
)""",
    """CREATE TABLE guideline_operations (
	operation_id UUID NOT NULL,
	idempotency_key UUID NOT NULL,
	kind TEXT NOT NULL,
	request_fingerprint TEXT NOT NULL,
	accepted_response JSONB NOT NULL,
	document_id UUID,
	version_id UUID,
	generation_id UUID,
	activation_epoch BIGINT,
	state TEXT NOT NULL,
	attempts INTEGER DEFAULT '0' NOT NULL,
	lease_owner TEXT,
	lease_until TIMESTAMP WITH TIME ZONE,
	next_attempt_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	safe_error_code TEXT,
	CONSTRAINT pk_guideline_operations PRIMARY KEY (operation_id),
	CONSTRAINT fk_guideline_operations_document_id_guideline_versions FOREIGN KEY(document_id, version_id) REFERENCES guideline_versions (document_id, version_id),
	CONSTRAINT fk_guideline_operations_document_id_guideline_documents FOREIGN KEY(document_id) REFERENCES guideline_documents (document_id),
	CONSTRAINT fk_guideline_operations_generation_id_guideline_index_g_07d5 FOREIGN KEY(generation_id) REFERENCES guideline_index_generations (generation_id),
	CONSTRAINT ck_guideline_operations_state CHECK (state IN ('pending', 'running', 'succeeded', 'failed', 'superseded')),
	CONSTRAINT ck_guideline_operations_attempts CHECK (attempts BETWEEN 0 AND 3),
	CONSTRAINT ck_guideline_operations_lease CHECK ((state = 'running' AND attempts > 0 AND lease_owner IS NOT NULL AND lease_until IS NOT NULL) OR (state <> 'running' AND lease_owner IS NULL AND lease_until IS NULL)),
	CONSTRAINT ck_guideline_operations_safe_error CHECK ((state = 'failed' AND safe_error_code IS NOT NULL AND safe_error_code ~ '^guideline_[a-z_]{1,80}$') OR (state <> 'failed' AND safe_error_code IS NULL)),
	CONSTRAINT ck_guideline_operations_uuid4 CHECK (substring(idempotency_key::text, 15, 1) = '4' AND substring(idempotency_key::text, 20, 1) IN ('8','9','a','b')),
	CONSTRAINT uq_guideline_operations_idempotency_key UNIQUE (idempotency_key)
)""",
    """ALTER TABLE guideline_documents ADD CONSTRAINT fk_guideline_documents_base FOREIGN KEY(family_root_id, based_on_version_id, base_content_digest) REFERENCES guideline_versions (document_id, version_id, content_digest)""",
    """ALTER TABLE guideline_documents ADD CONSTRAINT fk_guideline_documents_pending_version_id FOREIGN KEY(document_id, pending_version_id) REFERENCES guideline_versions (document_id, version_id) DEFERRABLE INITIALLY DEFERRED""",
    """ALTER TABLE guideline_documents ADD CONSTRAINT fk_guideline_documents_active_version_id FOREIGN KEY(document_id, active_version_id) REFERENCES guideline_versions (document_id, version_id) DEFERRABLE INITIALLY DEFERRED""",
    """CREATE UNIQUE INDEX uq_guideline_override_per_root ON guideline_documents (family_root_id) WHERE origin = 'local_override'""",
    """CREATE UNIQUE INDEX uq_guideline_generation_one_active ON guideline_index_generations (state) WHERE state = 'active'""",
    """CREATE UNIQUE INDEX uq_guideline_generation_one_building ON guideline_index_generations (state) WHERE state = 'building'""",
    """CREATE INDEX ix_guideline_operations_claim ON guideline_operations (state, next_attempt_at)""",
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public")
    for statement in _DDL:
        op.execute(statement)
    op.execute("INSERT INTO guideline_catalog_state (singleton_id, revision) VALUES (1, 0)")
    _install_guards()


def _install_guards() -> None:
    op.execute("""
CREATE FUNCTION guideline_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'guideline history cannot be deleted';
    END IF;
    IF TG_TABLE_NAME IN ('guideline_versions', 'guideline_embeddings') THEN
        RAISE EXCEPTION 'guideline snapshot is immutable';
    ELSIF TG_TABLE_NAME = 'guideline_operations' THEN
        IF (to_jsonb(NEW) - ARRAY['state','attempts','lease_owner','lease_until','next_attempt_at','safe_error_code','generation_id'])
           IS DISTINCT FROM
           (to_jsonb(OLD) - ARRAY['state','attempts','lease_owner','lease_until','next_attempt_at','safe_error_code','generation_id']) THEN
            RAISE EXCEPTION 'guideline receipt is immutable';
        END IF;
    ELSIF TG_TABLE_NAME = 'guideline_documents' THEN
        IF (to_jsonb(NEW) - ARRAY['draft','draft_digest','revision','activation_epoch','disabled','active_version_id','pending_version_id'])
           IS DISTINCT FROM
           (to_jsonb(OLD) - ARRAY['draft','draft_digest','revision','activation_epoch','disabled','active_version_id','pending_version_id'])
           OR NEW.revision < OLD.revision OR NEW.activation_epoch < OLD.activation_epoch THEN
            RAISE EXCEPTION 'guideline identity is immutable';
        END IF;
    ELSIF TG_TABLE_NAME = 'guideline_index_generations' THEN
        IF (to_jsonb(NEW) - ARRAY['state']) IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['state']) THEN
            RAISE EXCEPTION 'guideline profile is immutable';
        END IF;
    ELSIF TG_TABLE_NAME = 'guideline_catalog_entries' THEN
        IF (to_jsonb(NEW) - 'valid_to_revision') IS DISTINCT FROM (to_jsonb(OLD) - 'valid_to_revision')
           OR OLD.valid_to_revision IS NOT NULL OR NEW.valid_to_revision IS NULL THEN
            RAISE EXCEPTION 'guideline interval is immutable';
        END IF;
    ELSIF TG_TABLE_NAME = 'guideline_catalog_state' THEN
        IF NEW.singleton_id <> OLD.singleton_id OR NEW.revision < OLD.revision THEN
            RAISE EXCEPTION 'guideline catalog cannot move backwards';
        END IF;
    END IF;
    RETURN NEW;
END $$
""")
    for table in (
        "guideline_documents", "guideline_versions", "guideline_catalog_state",
        "guideline_catalog_entries", "guideline_index_generations", "guideline_embeddings",
        "guideline_operations",
    ):
        op.execute(f"CREATE TRIGGER {table}_guard BEFORE UPDATE OR DELETE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION guideline_guard()")


def downgrade() -> None:
    for table in ('guideline_operations', 'guideline_embeddings', 'guideline_catalog_entries',
                  'guideline_catalog_state'):
        op.execute(f'DROP TABLE {table}')
    for constraint in ('fk_guideline_documents_active_version_id',
                       'fk_guideline_documents_pending_version_id', 'fk_guideline_documents_base'):
        op.execute(f'ALTER TABLE guideline_documents DROP CONSTRAINT {constraint}')
    for table in ('guideline_versions', 'guideline_documents', 'guideline_index_generations'):
        op.execute(f'DROP TABLE {table}')
    op.execute('DROP FUNCTION guideline_guard()')
