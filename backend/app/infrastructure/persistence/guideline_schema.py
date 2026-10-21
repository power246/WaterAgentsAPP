from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, DateTime, ForeignKeyConstraint,
    Index, Integer, MetaData, Table, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint, JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncConnection
from app.infrastructure.persistence.guideline_relation_schema import define_relation_tables


def define_guideline_tables(metadata: MetaData) -> None:
    documents = Table(
        "guideline_documents", metadata,
        Column("document_id", UUID(as_uuid=True), primary_key=True),
        Column("family_root_id", UUID(as_uuid=True), nullable=False),
        Column("identity", JSONB, nullable=False),
        Column("origin", Text, nullable=False),
        Column("bundle_key", Text),
        Column("based_on_version_id", UUID(as_uuid=True)),
        Column("base_content_digest", Text),
        Column("draft", JSONB),
        Column("draft_digest", Text),
        Column("revision", BigInteger, nullable=False),
        Column("activation_epoch", BigInteger, nullable=False, server_default="0"),
        Column("disabled", Boolean, nullable=False, server_default="false"),
        Column("active_version_id", UUID(as_uuid=True)),
        Column("pending_version_id", UUID(as_uuid=True)),
        UniqueConstraint("document_id", "family_root_id"),
        UniqueConstraint("bundle_key"),
        CheckConstraint("revision >= 0 AND activation_epoch >= 0", name="revisions"),
        CheckConstraint("(draft IS NULL) = (draft_digest IS NULL)", name="draft_pair"),
        CheckConstraint("origin IN ('bundled', 'local_custom', 'local_override')", name="origin"),
        CheckConstraint(
            "(origin = 'local_override' AND document_id <> family_root_id "
            "AND based_on_version_id IS NOT NULL AND base_content_digest IS NOT NULL "
            "AND bundle_key IS NULL) OR (origin <> 'local_override' "
            "AND document_id = family_root_id AND based_on_version_id IS NULL "
            "AND base_content_digest IS NULL)", name="family_identity",
        ),
        CheckConstraint("(origin = 'bundled') = (bundle_key IS NOT NULL)", name="bundle_identity"),
        CheckConstraint("origin <> 'bundled' OR draft IS NULL", name="bundled_readonly"),
        ForeignKeyConstraint(["family_root_id"], ["guideline_documents.document_id"]),
    )
    Index("uq_guideline_override_per_root", documents.c.family_root_id, unique=True,
          postgresql_where=text("origin = 'local_override'"))
    versions = Table(
        "guideline_versions", metadata,
        Column("version_id", UUID(as_uuid=True), primary_key=True),
        Column("document_id", UUID(as_uuid=True), nullable=False),
        Column("family_root_id", UUID(as_uuid=True), nullable=False),
        Column("version_number", Integer, nullable=False),
        Column("content_digest", Text, nullable=False),
        Column("snapshot", JSONB, nullable=False),
        UniqueConstraint("document_id", "version_number", name="uq_guideline_version_number"),
        UniqueConstraint("document_id", "version_id", name="uq_guideline_version_owner"),
        UniqueConstraint("document_id", "version_id", "content_digest", name="uq_guideline_version_digest"),
        UniqueConstraint("family_root_id", "version_id"),
        CheckConstraint("version_number > 0", name="number"),
        ForeignKeyConstraint(["document_id", "family_root_id"],
                             ["guideline_documents.document_id", "guideline_documents.family_root_id"]),
    )
    for pointer in ("active_version_id", "pending_version_id"):
        documents.append_constraint(ForeignKeyConstraint(
            ["document_id", pointer], ["guideline_versions.document_id", "guideline_versions.version_id"],
            name=f"fk_guideline_documents_{pointer}", use_alter=True,
            deferrable=True, initially="DEFERRED",
        ))
    documents.append_constraint(ForeignKeyConstraint(
        ["family_root_id", "based_on_version_id", "base_content_digest"],
        ["guideline_versions.document_id", "guideline_versions.version_id", "guideline_versions.content_digest"],
        name="fk_guideline_documents_base", use_alter=True,
    ))
    generations = Table(
        "guideline_index_generations", metadata,
        Column("generation_id", UUID(as_uuid=True), primary_key=True),
        Column("profile", JSONB, nullable=False),
        Column("profile_id", Text, nullable=False),
        Column("profile_digest", Text, nullable=False),
        Column("dimensions", Integer, nullable=False),
        Column("state", Text, nullable=False),
        Column("catalog_revision", BigInteger, nullable=False),
        UniqueConstraint("generation_id", "profile_id", "dimensions"),
        CheckConstraint("state IN ('building', 'active', 'retired', 'failed')", name="state"),
        CheckConstraint("dimensions > 0 AND catalog_revision >= 0", name="bounds"),
        CheckConstraint("(profile->>'namespace' = 'guideline' AND profile->>'profile_id' = profile_id "
                        "AND profile->>'profile_digest' = profile_digest "
                        "AND (profile->>'output_dimensions')::integer = dimensions) IS TRUE", name="profile"),
    )
    for state in ("active", "building"):
        Index(f"uq_guideline_generation_one_{state}", generations.c.state, unique=True,
              postgresql_where=text(f"state = '{state}'"))
    Table(
        "guideline_catalog_state", metadata,
        Column("singleton_id", Integer, primary_key=True),
        Column("revision", BigInteger, nullable=False),
        Column("active_generation_id", UUID(as_uuid=True)),
        CheckConstraint("singleton_id = 1 AND revision >= 0", name="singleton"),
        ForeignKeyConstraint(["active_generation_id"], ["guideline_index_generations.generation_id"]),
    )
    entries = Table(
        "guideline_catalog_entries", metadata,
        Column("family_root_id", UUID(as_uuid=True), primary_key=True),
        Column("version_id", UUID(as_uuid=True), nullable=False),
        Column("valid_from_revision", BigInteger, primary_key=True),
        Column("valid_to_revision", BigInteger),
        CheckConstraint("valid_from_revision >= 0 AND (valid_to_revision IS NULL "
                        "OR valid_to_revision > valid_from_revision)", name="interval"),
        ForeignKeyConstraint(["family_root_id", "version_id"],
                             ["guideline_versions.family_root_id", "guideline_versions.version_id"]),
    )
    entries.append_constraint(ExcludeConstraint(
        (entries.c.family_root_id, "="),
        (func.int8range(entries.c.valid_from_revision, entries.c.valid_to_revision, "[)"), "&&"),
        name="ex_guideline_catalog_no_overlap", using="gist",
        ops={"family_root_id": "public.gist_uuid_ops"},
    ))
    Table(
        "guideline_embeddings", metadata,
        Column("generation_id", UUID(as_uuid=True), primary_key=True),
        Column("version_id", UUID(as_uuid=True), primary_key=True),
        Column("profile_id", Text, nullable=False),
        Column("dimensions", Integer, nullable=False),
        Column("canonical_document_digest", Text, nullable=False),
        Column("relation_count", Integer),
        Column("embedding", VECTOR(), nullable=False),
        ForeignKeyConstraint(["version_id"], ["guideline_versions.version_id"]),
        ForeignKeyConstraint(["generation_id", "profile_id", "dimensions"],
                             ["guideline_index_generations.generation_id", "guideline_index_generations.profile_id",
                              "guideline_index_generations.dimensions"]),
        CheckConstraint("public.vector_dims(embedding) = dimensions "
                        "AND public.vector_norm(embedding) BETWEEN 1e-12 AND 1e6", name="vector"),
        CheckConstraint("relation_count IS NULL OR relation_count > 0", name="relation_count"),
    )
    define_relation_tables(metadata)
    operations = Table(
        "guideline_operations", metadata,
        Column("operation_id", UUID(as_uuid=True), primary_key=True),
        Column("idempotency_key", UUID(as_uuid=True), nullable=False, unique=True),
        Column("kind", Text, nullable=False),
        Column("request_fingerprint", Text, nullable=False),
        Column("accepted_response", JSONB, nullable=False),
        Column("document_id", UUID(as_uuid=True)),
        Column("version_id", UUID(as_uuid=True)),
        Column("generation_id", UUID(as_uuid=True)),
        Column("activation_epoch", BigInteger),
        Column("state", Text, nullable=False),
        Column("attempts", Integer, nullable=False, server_default="0"),
        Column("lease_owner", Text),
        Column("lease_until", DateTime(timezone=True)),
        Column("next_attempt_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
        Column("safe_error_code", Text),
        ForeignKeyConstraint(["document_id", "version_id"],
                             ["guideline_versions.document_id", "guideline_versions.version_id"]),
        ForeignKeyConstraint(["document_id"], ["guideline_documents.document_id"]),
        ForeignKeyConstraint(["generation_id"], ["guideline_index_generations.generation_id"]),
        CheckConstraint("state IN ('pending', 'running', 'succeeded', 'failed', 'superseded')", name="state"),
        CheckConstraint("attempts BETWEEN 0 AND 3", name="attempts"),
        CheckConstraint("(state = 'running' AND attempts > 0 AND lease_owner IS NOT NULL "
                        "AND lease_until IS NOT NULL) OR (state <> 'running' AND lease_owner IS NULL "
                        "AND lease_until IS NULL)", name="lease"),
        CheckConstraint("(state = 'failed' AND safe_error_code IS NOT NULL AND safe_error_code ~ '^guideline_[a-z_]{1,80}$') "
                        "OR (state <> 'failed' AND safe_error_code IS NULL)", name="safe_error"),
        CheckConstraint("substring(idempotency_key::text, 15, 1) = '4' "
                        "AND substring(idempotency_key::text, 20, 1) IN ('8','9','a','b')", name="uuid4"),
    )
    Index("ix_guideline_operations_claim", operations.c.state, operations.c.next_attempt_at)


async def guideline_schema_matches(connection: AsyncConnection, metadata: MetaData) -> bool:
    tables = {name: table for name, table in metadata.tables.items() if name.startswith("guideline_")}
    rows = (await connection.execute(text(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name LIKE 'guideline_%'"
    ))).all()
    columns: dict[str, set[str]] = {}
    for table, column in rows:
        columns.setdefault(table, set()).add(column)
    if columns != {name: set(table.c.keys()) for name, table in tables.items()}:
        return False
    constraints = set((await connection.execute(text(
        "SELECT c.conname FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
        "WHERE n.nspname = current_schema()"
    ))).scalars())
    required = {connection.dialect.identifier_preparer.format_constraint(c)
                for table in tables.values() for c in table.constraints}
    indexes = set((await connection.execute(text(
        "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
    ))).scalars())
    triggers = set((await connection.execute(text(
        "SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND t.tgenabled = 'O'"
    ))).scalars())
    return (required <= constraints
            and {str(i.name) for table in tables.values() for i in table.indexes} <= indexes
            and {name + "_guard" for name in tables} <= triggers
            and "guideline_relation_completeness" in triggers
            and "guideline_relation_link_completeness" in triggers
            and await connection.scalar(text(
                "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'btree_gist')"
            )))
