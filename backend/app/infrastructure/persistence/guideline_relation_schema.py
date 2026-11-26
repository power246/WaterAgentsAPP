from pgvector.sqlalchemy import VECTOR
from sqlalchemy import Column, Integer, Text, Table, ForeignKeyConstraint, CheckConstraint
from sqlalchemy.dialects.postgresql import UUID


def define_relation_tables(metadata):
    Table("guideline_relation_embeddings", metadata,
        Column("generation_id", UUID(as_uuid=True), primary_key=True),
        Column("fragment_digest", Text, primary_key=True),
        Column("profile_id", Text, nullable=False),
        Column("dimensions", Integer, nullable=False),
        Column("relation", Text, nullable=False),
        Column("embedding", VECTOR(), nullable=False),
        ForeignKeyConstraint(["generation_id", "profile_id", "dimensions"],
            ["guideline_index_generations.generation_id", "guideline_index_generations.profile_id", "guideline_index_generations.dimensions"],
            name="fk_guideline_relation_generation"),
        CheckConstraint("public.vector_dims(embedding) = dimensions AND public.vector_norm(embedding) BETWEEN 1e-12 AND 1e6", name="vector"))
    Table("guideline_relation_links", metadata,
        Column("generation_id", UUID(as_uuid=True), primary_key=True),
        Column("version_id", UUID(as_uuid=True), primary_key=True),
        Column("fragment_digest", Text, primary_key=True),
        Column("locator", Text, primary_key=True),
        ForeignKeyConstraint(["generation_id", "fragment_digest"],
            ["guideline_relation_embeddings.generation_id", "guideline_relation_embeddings.fragment_digest"], name="fk_guideline_relation_link_fragment"),
        ForeignKeyConstraint(["generation_id", "version_id"],
            ["guideline_embeddings.generation_id", "guideline_embeddings.version_id"],
            name="fk_guideline_relation_link_version", deferrable=True, initially="DEFERRED"))
