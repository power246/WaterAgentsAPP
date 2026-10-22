"""Versioned many-to-many guideline relation vectors and atomic readiness."""
from alembic import op

revision = "20261021_01"
down_revision = "20261020_02"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE guideline_embeddings ADD COLUMN relation_count INTEGER")
    op.execute("ALTER TABLE guideline_embeddings ADD CONSTRAINT ck_guideline_embeddings_relation_count CHECK (relation_count IS NULL OR relation_count > 0)")
    op.execute("""CREATE TABLE guideline_relation_embeddings (
        generation_id UUID NOT NULL, fragment_digest TEXT NOT NULL, profile_id TEXT NOT NULL,
        dimensions INTEGER NOT NULL, relation TEXT NOT NULL, embedding public.vector NOT NULL,
        CONSTRAINT pk_guideline_relation_embeddings PRIMARY KEY (generation_id, fragment_digest),
        CONSTRAINT fk_guideline_relation_generation FOREIGN KEY (generation_id, profile_id, dimensions)
            REFERENCES guideline_index_generations (generation_id, profile_id, dimensions),
        CONSTRAINT ck_guideline_relation_embeddings_vector CHECK (public.vector_dims(embedding) = dimensions
            AND public.vector_norm(embedding) BETWEEN 1e-12 AND 1e6))""")
    op.execute("""CREATE TABLE guideline_relation_links (
        generation_id UUID NOT NULL, version_id UUID NOT NULL, fragment_digest TEXT NOT NULL, locator TEXT NOT NULL,
        CONSTRAINT pk_guideline_relation_links PRIMARY KEY (generation_id, version_id, fragment_digest, locator),
        CONSTRAINT fk_guideline_relation_link_fragment FOREIGN KEY (generation_id, fragment_digest)
            REFERENCES guideline_relation_embeddings (generation_id, fragment_digest),
        CONSTRAINT fk_guideline_relation_link_version FOREIGN KEY (generation_id, version_id)
            REFERENCES guideline_embeddings (generation_id, version_id) DEFERRABLE INITIALLY DEFERRED)""")
    op.execute("""CREATE FUNCTION guideline_relation_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'guideline relation history is immutable'; END $$""")
    for table in ("guideline_relation_embeddings", "guideline_relation_links"):
        op.execute(f"CREATE TRIGGER {table}_guard BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION guideline_relation_immutable()")
    op.execute("""CREATE FUNCTION guideline_relation_complete() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE template INTEGER; declared INTEGER;
        BEGIN
            SELECT (g.profile->>'document_template_version')::integer, b.relation_count INTO template, declared
                FROM guideline_index_generations g JOIN guideline_embeddings b USING (generation_id)
                WHERE g.generation_id = NEW.generation_id AND b.version_id = NEW.version_id;
            IF template = 3 AND (declared IS NULL OR declared < 1 OR declared <>
                (SELECT count(*) FROM guideline_relation_links WHERE generation_id = NEW.generation_id AND version_id = NEW.version_id)) THEN
                RAISE EXCEPTION 'guideline relation index is incomplete';
            END IF;
            RETURN NEW;
        END $$""")
    op.execute("CREATE CONSTRAINT TRIGGER guideline_relation_completeness AFTER INSERT ON guideline_embeddings DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION guideline_relation_complete()")
    op.execute("CREATE CONSTRAINT TRIGGER guideline_relation_link_completeness AFTER INSERT ON guideline_relation_links DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION guideline_relation_complete()")


def downgrade():
    op.execute("DROP TRIGGER guideline_relation_completeness ON guideline_embeddings")
    op.execute("DROP TRIGGER guideline_relation_link_completeness ON guideline_relation_links")
    op.execute("DROP FUNCTION guideline_relation_complete()")
    op.execute("DROP TABLE guideline_relation_links")
    op.execute("DROP TABLE guideline_relation_embeddings")
    op.execute("DROP FUNCTION guideline_relation_immutable()")
    op.execute("ALTER TABLE guideline_embeddings DROP CONSTRAINT ck_guideline_embeddings_relation_count")
    op.execute("ALTER TABLE guideline_embeddings DROP COLUMN relation_count")
