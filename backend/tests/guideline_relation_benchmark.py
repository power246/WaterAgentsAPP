"""Synthetic, isolated-PostgreSQL comparison; never a production bundle loader."""
import asyncio
import json
import os
from pathlib import Path
from time import perf_counter
from hashlib import sha256
from uuid import UUID

from sqlalchemy import text

from app.application.guidelines.embedding import desired_guideline_profile, distinct_query_vectors
from app.domain import ModelCatalogSnapshot
from app.domain.guidelines import canonical_guideline_document, CanonicalGuidelineQueryV1, GuidelineEmbeddingProfileV1, GuidelineIdentity
from app.infrastructure.config import load_parameter_capability_registry
from app.infrastructure.persistence.guideline_repository import PostgresGuidelineRepository
from tests.guideline_support import content, version, PH
from tests.persistence.test_guidelines import active_generation, create, publish

CORPUS = Path(__file__).parent / "fixtures/guidelines/synthetic_relation_corpus.json"


async def evaluate_synthetic_relations(settings, preparation_port, query_port=None):
    fixture = json.loads(CORPUS.read_text(encoding="utf-8"))
    if fixture.get("synthetic") is not True or not all(d["id"].startswith("synthetic.") for d in fixture["documents"]):
        raise ValueError("comparison accepts synthetic fixtures only")
    if settings.db_name != os.environ.get("WATERAGENTS_TEST_DB_NAME"):
        raise ValueError("comparison requires the explicit isolated test database")
    query_port = query_port or preparation_port
    registry = load_parameter_capability_registry()
    current = desired_guideline_profile(ModelCatalogSnapshot(capabilities=settings.model_catalog), settings.guideline_policy)
    baseline = GuidelineEmbeddingProfileV1.model_validate(current.model_dump(exclude={"profile_id", "profile_digest"}) | {
        "text_contract_version": 2, "document_template_version": 2,
        "document_instructions": "Represent this water-quality advisory title, summary, parameters, scenarios, and keywords for retrieval.",
    })
    requests, embedded = 0, 0

    async def embed(port, profile, inputs, *, online=False):
        nonlocal requests, embedded
        requests += 1
        embedded += len(inputs)
        result = await port.embed(profile, inputs, asyncio.get_running_loop().time()+(3 if online else 20), None)
        if result.profile != profile or len(result.vectors) != len(inputs):
            raise ValueError("synthetic comparison embedding batch mismatch")
        return result.vectors

    repository = PostgresGuidelineRepository(settings)
    report = {"synthetic": True, "documents": len(fixture["documents"]), "queries": len(fixture["queries"]),
        "profile": current.model_dump(mode="json"), "cutoff": settings.guideline_policy.semantic_min_similarity,
        "identity_policy": "UUIDv4 bits from SHA256 of fixture id; stable ties across isolated runs",
        "arms": {}, "limits": "Synthetic fixture quality only; not an approved pH corpus or real-world accuracy claim."}
    try:
        generation = await active_generation(repository, baseline)
        contents = [content(title=d["title"], summary=d["summary"], body_markdown="\n\n".join(d["paragraphs"])) for d in fixture["documents"]]
        documents = tuple(canonical_guideline_document(c, baseline, registry) for c in contents)
        baseline_vectors = await embed(preparation_port, baseline, documents)
        versions, names = [], {}
        for entry, selected, document, vector in zip(fixture["documents"], contents, documents, baseline_vectors, strict=True):
            fixed_id = UUID(bytes=sha256(entry["id"].encode()).digest()[:16], version=4)
            identity = GuidelineIdentity(guideline_id=fixed_id, family_root_id=fixed_id, origin="local_custom")
            identity, receipt = await create(repository, selected_identity=identity, selected_content=selected)
            approved = version(selected, identity)
            await publish(repository, approved, receipt.accepted_revision)
            claim = await repository.claim_operation("synthetic-comparison-worker")
            await repository.complete_operation(claim, generation.generation_id, document, vector)
            versions.append(approved)
            names[approved.version_id] = entry["id"]
        baseline_catalog = await repository.read_catalog()
        next_generation = await repository.create_generation(current)
        manifests = [canonical_guideline_document(v.content, current, registry) for v in versions]
        unique = {f.canonical_digest: f for d in manifests for f in d.fragments}
        relation_vectors = {}
        values = tuple(unique.values())
        for offset in range(0, len(values), 16):
            batch = values[offset:offset+16]
            vectors = await embed(preparation_port, current, batch)
            relation_vectors.update(zip((f.canonical_digest for f in batch), vectors, strict=True))
        for approved, document in zip(versions, manifests, strict=True):
            await repository.write_building_embedding(next_generation.generation_id, approved.version_id, document,
                tuple(relation_vectors[f.canonical_digest] for f in document.fragments))
        await repository.activate_generation(next_generation.generation_id, expected_catalog_revision=baseline_catalog.revision)
        relation_catalog = await repository.read_catalog()
        for arm in ("summary_single", "relations_single", "relations_aspects"):
            selected_profile = baseline if arm == "summary_single" else current
            snapshot = baseline_catalog if arm == "summary_single" else relation_catalog
            rows = []
            for case in fixture["queries"]:
                questions = case["aspects"] if arm == "relations_aspects" else ["; ".join(case["aspects"])]
                queries = tuple(CanonicalGuidelineQueryV1(profile_id=selected_profile.profile_id,
                    instructions=selected_profile.query_instructions, parameter_keys=(PH,), mode="prediction", question=q) for q in questions)
                started = perf_counter()
                vectors = await embed(query_port, selected_profile, queries, online=True)
                vectors = distinct_query_vectors(vectors)
                matches = await repository.rank_guidelines(snapshot, (PH,), vectors[0] if len(vectors) == 1 else vectors,
                    cutoff=settings.guideline_policy.semantic_min_similarity, limit=3)
                found = [names[m.version.version_id] for m in matches]
                expected = set(case["expected_ids"])
                rows.append({"id": case["id"], "question": case["question"], "expected": sorted(expected),
                    "query_aspects": questions, "distinct_query_vectors": len(vectors),
                    "ranking": found, "scores": [m.similarity for m in matches], "seconds": perf_counter()-started,
                    "hit_at_1": bool(found and found[0] in expected), "hit_at_3": bool(set(found) & expected),
                    "recall_at_3": len(set(found) & expected)/len(expected)})
            report["arms"][arm] = {"hit_at_1": sum(r["hit_at_1"] for r in rows),
                "hit_at_3": sum(r["hit_at_3"] for r in rows),
                "mean_recall_at_3": sum(r["recall_at_3"] for r in rows)/len(rows),
                "mean_seconds": sum(r["seconds"] for r in rows)/len(rows), "cases": rows}
        async with repository._engine.connect() as connection:
            report["unique_relation_vectors"] = await connection.scalar(text("SELECT count(*) FROM guideline_relation_embeddings"))
            report["relation_version_links"] = await connection.scalar(text("SELECT count(*) FROM guideline_relation_links"))
        report.update(provider_requests=requests, embedded_inputs=embedded, status="completed")
        return report
    finally:
        await repository.aclose()
