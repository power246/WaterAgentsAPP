import json

from sqlalchemy import text

from app.domain.guidelines.canonical import canonical_text
from app.domain.guidelines.canonical_fragment import CanonicalGuidelineFragmentV3
from app.domain.guidelines.embedding_batch import GuidelineEmbeddingBatch
from app.domain.guidelines.match import GuidelineMatch
from app.domain.guidelines.version import GuidelineVersionV1
from app.domain.guidelines.canonical_document import canonical_guideline_document
from app.domain.memory_rag.embedding_vector import EmbeddingVector
from app.ports.errors import GuidelineConflictError


async def read_relation_embeddings(connection, generation_id, profile, fragments):
    expected = {fragment.canonical_digest: fragment for fragment in fragments}
    for fragment in fragments:
        fragment.require_profile(profile)
    rows = (await connection.execute(text(
        "SELECT fragment_digest, profile_id, dimensions, relation, embedding::text AS vector "
        "FROM guideline_relation_embeddings WHERE generation_id = :gen "
        "AND fragment_digest IN (SELECT jsonb_array_elements_text(CAST(:ids AS jsonb)))"
    ), {"gen": generation_id, "ids": canonical_text(list(expected))})).mappings().all()
    result = {}
    for row in rows:
        fragment = CanonicalGuidelineFragmentV3(profile_id=row["profile_id"],
            instructions=profile.document_instructions, relation=row["relation"])
        if (fragment != expected[row["fragment_digest"]] or fragment.canonical_digest != row["fragment_digest"]
                or row["dimensions"] != profile.output_dimensions):
            raise GuidelineConflictError("guideline_embedding_conflict")
        vector = EmbeddingVector(values=tuple(json.loads(row["vector"])))
        GuidelineEmbeddingBatch(profile=profile, vectors=(vector,))
        result[row["fragment_digest"]] = vector
    return result


async def write_relations(connection, generation_id, version_id, profile, document, vectors):
    for fragment, vector in zip(document.fragments, vectors, strict=True):
        await connection.execute(text(
            "INSERT INTO guideline_relation_embeddings (generation_id, fragment_digest, profile_id, dimensions, relation, embedding) "
            "VALUES (:gen, :id, :profile, :dim, :relation, CAST(:vector AS public.vector)) ON CONFLICT DO NOTHING"
        ), {"gen": generation_id, "id": fragment.canonical_digest, "profile": profile.profile_id,
            "dim": profile.output_dimensions, "relation": fragment.relation, "vector": canonical_text(list(vector.values))})
    for fragment_id, locator in document.links:
        await connection.execute(text(
            "INSERT INTO guideline_relation_links (generation_id, version_id, fragment_digest, locator) "
            "VALUES (:gen, :version, :fragment, :locator) ON CONFLICT DO NOTHING"
        ), {"gen": generation_id, "version": version_id, "fragment": fragment_id, "locator": locator})
    actual = tuple((await connection.execute(text(
        "SELECT fragment_digest, locator FROM guideline_relation_links WHERE generation_id = :gen AND version_id = :version "
        "ORDER BY fragment_digest, locator"
    ), {"gen": generation_id, "version": version_id})).tuples())
    if actual != document.links:
        raise GuidelineConflictError("guideline_embedding_conflict")
    saved = await read_relation_embeddings(connection, generation_id, profile, document.fragments)
    return saved[document.fragments[0].canonical_digest]


async def rank_relations(connection, pinned_catalog_sql, parameters, snapshot, vectors, cutoff, limit, registry):
    queries = " UNION ALL ".join(f"SELECT {i} AS aspect, CAST(:vector{i} AS public.vector) AS vector" for i in range(len(vectors)))
    rows = (await connection.execute(text(
        "WITH catalog AS (" + pinned_catalog_sql + "), "
        "eligible AS MATERIALIZED (SELECT * FROM catalog WHERE eligible AND embedding IS NOT NULL), "
        "queries AS (" + queries + "), "
        "scores AS (SELECT e.version_id, r.fragment_digest, q.aspect, greatest(-1.0, least(1.0, "
        "1 - (r.embedding OPERATOR(public.<=>) q.vector))) AS similarity "
        "FROM eligible e JOIN guideline_relation_links l ON l.version_id = e.version_id AND l.generation_id = :generation "
        "JOIN guideline_relation_embeddings r ON r.generation_id = l.generation_id AND r.fragment_digest = l.fragment_digest "
        "CROSS JOIN queries q), "
        "best_aspects AS (SELECT DISTINCT ON (version_id, aspect) * FROM scores WHERE similarity >= :cutoff "
        "ORDER BY version_id, aspect, similarity DESC, fragment_digest), "
        "independent AS (SELECT DISTINCT ON (version_id, fragment_digest) * FROM best_aspects "
        "ORDER BY version_id, fragment_digest, similarity DESC, aspect), "
        "aggregated AS (SELECT version_id, CASE WHEN :query_count = 1 THEN max(similarity) ELSE max(similarity)*0.85 + 0.15*"
        "((sum(similarity)-max(similarity))/:extra_aspects) END AS relevance FROM independent GROUP BY version_id) "
        "SELECT e.*, a.relevance AS similarity FROM eligible e JOIN aggregated a ON a.version_id = e.version_id "
        "ORDER BY a.relevance DESC, e.family_root_id, e.version_id LIMIT :limit"
    ), {**parameters, **{f"vector{i}": canonical_text(list(vector.values)) for i, vector in enumerate(vectors)},
        "cutoff": cutoff, "limit": limit, "extra_aspects": max(1, len(vectors)-1), "query_count": len(vectors)})).mappings().all()
    matches = []
    for row in rows:
        version = GuidelineVersionV1.model_validate(row["snapshot"])
        document = canonical_guideline_document(version.content, snapshot.profile, registry)
        if document.canonical_digest != row["canonical_document_digest"]:
            raise GuidelineConflictError("guideline_embedding_conflict")
        links = tuple((await connection.execute(text(
            "SELECT fragment_digest, locator FROM guideline_relation_links WHERE generation_id = :gen AND version_id = :version "
            "ORDER BY fragment_digest, locator"
        ), {"gen": snapshot.generation_id, "version": version.version_id})).tuples())
        if links != document.links:
            raise GuidelineConflictError("guideline_embedding_conflict")
        await read_relation_embeddings(connection, snapshot.generation_id, snapshot.profile, document.fragments)
        matches.append(GuidelineMatch(version=version, similarity=row["similarity"]))
    return tuple(matches)
