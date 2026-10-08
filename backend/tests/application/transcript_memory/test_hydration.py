import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.application.transcript_memory import (
    ConfirmedSourceReference,
    GenerationService,
    RAGSourceReadError,
    RAGSearchInvalidError,
    RAGSearchService,
    RAGSearchUnavailableError,
    build_prepared_projection,
)
from app.application.transcript_memory.search_policies import rank_projection
from app.domain import (
    EmbeddingBatch,
    EmbeddingProfile,
    EmbeddingVector,
    HistoricalSearchIntentV1,
    HistoricalSearchStrategy,
    MemoryMode,
    RAGIndexGeneration,
    RAGIndexGenerationStatus,
    RAGQualityTier,
)
from app.ports import PortTimeoutError
from tests.application.evidence_tools.conftest import (
    TARGET_PARAMETER,
    make_context,
    make_scope,
)
from tests.application.transcript_memory.m3_support import case_source

GENERATION_ID = "87000000-0000-4000-8000-000000000001"
NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)


def _profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        provider="test-provider",
        model="embedding-3",
        output_dimensions=3,
        max_input_tokens=512,
        semantic_min_similarity=0.75,
    )


def _generation() -> RAGIndexGeneration:
    return RAGIndexGeneration(
        generation_id=GENERATION_ID,
        profile=_profile(),
        status=RAGIndexGenerationStatus.ACTIVE,
        source_cutoff_created_at=NOW,
        source_cutoff_item_id="87000000-0000-4000-8000-000000000002",
        created_at=NOW,
        activated_at=NOW,
    )


def _with_run_id(source, run_id: UUID):
    def move_item(item, *, body=None):
        return replace(
            item,
            item=item.item.model_copy(update={"run_id": run_id}),
            body=item.body if body is None else body,
        )

    result_body = source.result.body.model_copy(update={"run_id": run_id})
    session = source.session.model_copy(
        update={
            "result_ref": source.session.result_ref.model_copy(
                update={"run_id": run_id}
            )
        }
    )
    return replace(
        source,
        session=session,
        analysis_context=move_item(source.analysis_context),
        topology=move_item(source.topology),
        result=move_item(source.result, body=result_body),
        fragments=tuple(move_item(item) for item in source.fragments),
        confirmed_references=tuple(
            replace(item, run_id=run_id) for item in source.confirmed_references
        ),
    )


class _GenerationStore:
    def __init__(self, generation=None) -> None:
        self.active = generation or _generation()

    async def read_active_generation(self):
        return self.active

    async def read_generation(self, generation_id):
        return self.active if self.active.generation_id == generation_id else None

    async def delete_generation(self, generation_id):
        raise AssertionError("search must not delete generations")


class _Embedding:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = []

    async def embed(self, profile, texts, deadline, cancel_token):
        self.calls.append((profile, texts, deadline, cancel_token))
        if self.error is not None:
            raise self.error
        return EmbeddingBatch(
            profile=profile,
            vectors=(EmbeddingVector(values=(0.2, 0.3, 0.4)),),
        )


class _SourceLoader:
    def __init__(self, sources) -> None:
        self.sources = sources
        self.calls = []

    async def load_historical_case_source(self, case_ref):
        self.calls.append(case_ref)
        source = self.sources
        if isinstance(source, Exception):
            raise source
        if isinstance(source, dict):
            return source[(case_ref.session_id, case_ref.result_item_id)]
        return source


class _Index:
    def __init__(self, sources, *, corrupt_digest: bool = False) -> None:
        self.sources = sources if isinstance(sources, tuple) else (sources,)
        self.corrupt_digest = corrupt_digest
        self.queries = []

    async def search_candidates(self, query):
        self.queries.append(query)
        candidates = []
        for source in self.sources:
            projection = build_prepared_projection(
                source,
                generation_id=query.generation_id,
                profile_id=query.profile_id,
            ).case
            quality = (
                RAGQualityTier.SUSPICIOUS
                if source.fragments
                and source.fragments[0].body.quality_class.value == "suspicious"
                else RAGQualityTier.VALID
            )
            candidate = rank_projection(
                query,
                projection,
                lexical_similarity=0.2,
                vector_similarity=(
                    0.9
                    if query.intent.strategy
                    is HistoricalSearchStrategy.SEMANTIC_CASE_QUERY
                    else None
                ),
                quality_tier=quality,
                original_confidence=projection.result_projection.overall_confidence,
            )
            if self.corrupt_digest:
                candidate = candidate.model_copy(
                    update={"projection_digest": "sha256:corrupt"}
                )
            candidates.append(candidate)
        return tuple(candidates)


class _RepeatingIndex(_Index):
    def __init__(self, source, count: int) -> None:
        super().__init__(source)
        self.count = count

    async def search_candidates(self, query):
        candidate = (await super().search_candidates(query))[0]
        return tuple(candidate for _ in range(self.count))


def _fixture(*, embedding=None, source=None, preview_max_bytes=16_384):
    context = make_context(memory_mode=MemoryMode.MEMORY_AUGMENTED)
    scope = make_scope(context=context, with_upstream=True)
    source = source or case_source(context=context)
    loader_sources = source
    if isinstance(source, tuple):
        loader_sources = {
            (str(item.session.session_id), str(item.result.item.item_id)): item
            for item in source
        }
    loader = _SourceLoader(loader_sources)
    index = _Index(source)
    service = RAGSearchService(
        generations=GenerationService(_GenerationStore()),
        index=index,
        source_loader=loader,
        embedding=embedding,
        preview_max_bytes=preview_max_bytes,
    )
    return scope, service.bind(scope), index, loader


def _intent(scope, strategy):
    return HistoricalSearchIntentV1(
        strategy=strategy,
        parameter_keys=(TARGET_PARAMETER,),
        window_ids=tuple(window.window_id for window in scope.time_windows),
    )


def test_hydrator_returns_only_authorized_cells_and_direct_fragments() -> None:
    async def exercise() -> None:
        scope, session, index, loader = _fixture()

        records = await session.search(
            _intent(scope, HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW),
            operation_id="operation-hydrate",
        )

        assert len(records) == 1
        record = records[0]
        assert record.case_ref == loader.calls[0]
        assert {cell.parameter for cell in record.result_projection.cells} == {
            TARGET_PARAMETER
        }
        assert len(record.supporting_fragments) == 1
        assert record.supporting_fragments[0].original_item_id == str(
            loader.sources.fragments[0].item.item_id
        )
        assert all(
            fragment.applicability.value == "contextual_only"
            for fragment in record.supporting_fragments
        )
        assert len(record.model_dump_json().encode("utf-8")) <= 16_384
        assert index.queries[0].generation_id == GENERATION_ID

    asyncio.run(exercise())


def test_hydrator_ignores_ineligible_referenced_fragments() -> None:
    async def exercise() -> None:
        source = case_source(
            context=make_context(memory_mode=MemoryMode.MEMORY_AUGMENTED)
        )
        ineligible_id = UUID("87000000-0000-4000-8000-000000000088")
        ineligible = replace(
            source.fragments[0],
            item=source.fragments[0].item.model_copy(
                update={
                    "item_id": ineligible_id,
                    "transcript_sequence": 10,
                    "rag_eligible": False,
                }
            ),
        )
        mixed = replace(
            source,
            result=replace(
                source.result,
                references=(*source.result.references, ineligible_id),
            ),
            fragments=(*source.fragments, ineligible),
            confirmed_references=(
                *source.confirmed_references,
                ConfirmedSourceReference(
                    run_id=ineligible.item.run_id,
                    item_id=ineligible_id,
                    session_id=ineligible.item.session_id,
                    content_type="evidence_fragment",
                    content_schema_version=1,
                ),
            ),
        )
        scope, session, _, _ = _fixture(source=mixed)

        records = await session.search(
            _intent(scope, HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW),
            operation_id="operation-mixed-eligibility",
        )

        assert len(records) == 1
        assert tuple(
            fragment.original_item_id
            for fragment in records[0].supporting_fragments
        ) == (str(source.fragments[0].item.item_id),)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "strategy",
    (
        HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW,
        HistoricalSearchStrategy.NEARBY_COMID_PARAMETER,
        HistoricalSearchStrategy.PARAMETER_SEASON,
        HistoricalSearchStrategy.TOPOLOGY_NEIGHBORHOOD_PARAMETER,
    ),
)
def test_structured_strategies_do_not_require_embedding_supplier(strategy) -> None:
    async def exercise() -> None:
        source = None
        if strategy is HistoricalSearchStrategy.TOPOLOGY_NEIGHBORHOOD_PARAMETER:
            source = case_source(
                context=make_context(memory_mode=MemoryMode.MEMORY_AUGMENTED),
                target_comid="200",
            )
        scope, session, _, _ = _fixture(embedding=None, source=source)

        records = await session.search(
            _intent(scope, strategy),
            operation_id=f"structured-{strategy.value}",
        )

        assert isinstance(records, tuple)

    asyncio.run(exercise())


def test_semantic_embedding_success_is_cached_once_but_failure_is_not_cached() -> None:
    async def exercise() -> None:
        embedding = _Embedding()
        scope, session, _, _ = _fixture(embedding=embedding)
        intent = _intent(scope, HistoricalSearchStrategy.SEMANTIC_CASE_QUERY)

        assert await session.search(intent, operation_id="semantic-1")
        assert await session.search(intent, operation_id="semantic-2")
        assert len(embedding.calls) == 1

        failure = _Embedding(
            PortTimeoutError("timeout", provider="test", operation="embedding")
        )
        _, failed_session, _, _ = _fixture(embedding=failure)
        for _ in range(2):
            with pytest.raises(RAGSearchUnavailableError, match="embedding_unavailable"):
                await failed_session.search(intent, operation_id="semantic-failed")
        assert len(failure.calls) == 2

    asyncio.run(exercise())


def test_semantic_query_embedding_is_query_to_query_across_multi_waterbody_hits() -> None:
    async def exercise() -> None:
        context = make_context(memory_mode=MemoryMode.MEMORY_AUGMENTED)
        sources = (
            case_source(context=context),
            _with_run_id(
                case_source(
                    context=context,
                    target_comid="200",
                    session_id=UUID("81000000-0000-4000-8000-000000000020"),
                    fragment_item_id=UUID(
                        "81000000-0000-4000-8000-000000000021"
                    ),
                    result_item_id=UUID(
                        "81000000-0000-4000-8000-000000000022"
                    ),
                    source_record_item_id=UUID(
                        "81000000-0000-4000-8000-000000000023"
                    ),
                ),
                UUID("89000000-0000-4000-8000-000000000001"),
            ),
        )
        embedding = _Embedding()
        scope, session, index, _ = _fixture(source=sources, embedding=embedding)

        records = await session.search(
            _intent(scope, HistoricalSearchStrategy.SEMANTIC_CASE_QUERY),
            operation_id="semantic-multi-waterbody",
        )

        assert {record.case_ref.target_comid for record in records} == {"123", "200"}
        assert len({record.case_ref.run_id for record in records}) == 2
        assert len(embedding.calls) == 1
        embedded_text = embedding.calls[0][1][0]
        assert all(
            source.result.body.result_id not in embedded_text for source in sources
        )
        assert index.queries[0].query_embedding == EmbeddingVector(
            values=(0.2, 0.3, 0.4)
        )

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "corruption",
    (
        "source_deleted",
        "schema",
        "session_result",
        "comid",
        "projection_digest",
        "fragment_ref",
        "source_eligibility",
    ),
)
def test_any_top_hit_corruption_invalidates_the_whole_search(corruption) -> None:
    async def exercise() -> None:
        context = make_context(memory_mode=MemoryMode.MEMORY_AUGMENTED)
        source = case_source(context=context)
        if corruption == "source_deleted":
            hydrated_source = RAGSourceReadError("historical_source_missing")
        elif corruption == "schema":
            hydrated_source = RAGSourceReadError(
                "historical_source_schema_unsupported"
            )
        elif corruption == "session_result":
            hydrated_source = case_source(
                context=context,
                session_id=UUID("85000000-0000-4000-8000-000000000001"),
                fragment_item_id=UUID(
                    "85000000-0000-4000-8000-000000000002"
                ),
                result_item_id=UUID("85000000-0000-4000-8000-000000000003"),
                source_record_item_id=UUID(
                    "85000000-0000-4000-8000-000000000004"
                ),
            )
        elif corruption == "comid":
            hydrated_source = case_source(context=context, target_comid="200")
        elif corruption == "fragment_ref":
            hydrated_source = replace(source, fragments=())
        elif corruption == "source_eligibility":
            hydrated_source = replace(source, persistence_confirmed=False)
        else:
            hydrated_source = source
        scope = make_scope(context=context, with_upstream=True)
        loader = _SourceLoader(hydrated_source)
        index = _Index(source, corrupt_digest=corruption == "projection_digest")
        session = RAGSearchService(
            generations=GenerationService(_GenerationStore()),
            index=index,
            source_loader=loader,
            embedding=None,
        ).bind(scope)

        with pytest.raises(RAGSearchInvalidError):
            await session.search(
                _intent(
                    scope,
                    HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW,
                ),
                operation_id="corrupt-hit",
            )

    asyncio.run(exercise())


def test_diversity_limits_two_cases_per_source_run_and_never_truncates_json() -> None:
    async def exercise() -> None:
        context = make_context(memory_mode=MemoryMode.MEMORY_AUGMENTED)
        sources = tuple(
            case_source(
                context=context,
                session_id=UUID(f"81000000-0000-4000-8000-{index:012d}"),
                fragment_item_id=UUID(f"82000000-0000-4000-8000-{index:012d}"),
                result_item_id=UUID(f"83000000-0000-4000-8000-{index:012d}"),
                source_record_item_id=UUID(
                    f"84000000-0000-4000-8000-{index:012d}"
                ),
            )
            for index in range(1, 4)
        )
        scope, session, _, loader = _fixture(source=sources)

        records = await session.search(
            _intent(scope, HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW),
            operation_id="diversity",
        )

        assert len(records) == 2
        assert len(loader.calls) == 2
        assert all(record.model_dump_json().startswith("{") for record in records)

        _, tiny_session, _, _ = _fixture(preview_max_bytes=2)
        with pytest.raises(RAGSearchInvalidError, match="result_too_large"):
            await tiny_session.search(
                _intent(
                    scope,
                    HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW,
                ),
                operation_id="too-large",
            )

    asyncio.run(exercise())


def test_session_result_dedupe_and_internal_candidate_cap_fail_closed() -> None:
    async def exercise() -> None:
        context = make_context(memory_mode=MemoryMode.MEMORY_AUGMENTED)
        source = case_source(context=context)
        scope = make_scope(context=context, with_upstream=True)
        loader = _SourceLoader(source)
        duplicate_session = RAGSearchService(
            generations=GenerationService(_GenerationStore()),
            index=_RepeatingIndex(source, 2),
            source_loader=loader,
            embedding=None,
        ).bind(scope)
        records = await duplicate_session.search(
            _intent(scope, HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW),
            operation_id="dedupe",
        )
        assert len(records) == 1
        assert len(loader.calls) == 1

        over_limit = RAGSearchService(
            generations=GenerationService(_GenerationStore()),
            index=_RepeatingIndex(source, 51),
            source_loader=_SourceLoader(source),
            embedding=None,
        ).bind(scope)
        with pytest.raises(RAGSearchInvalidError, match="candidate_limit_exceeded"):
            await over_limit.search(
                _intent(
                    scope,
                    HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW,
                ),
                operation_id="over-limit",
            )

    asyncio.run(exercise())


def test_captured_generation_remains_pinned_across_concurrent_profile_switch() -> None:
    async def exercise() -> None:
        context = make_context(memory_mode=MemoryMode.MEMORY_AUGMENTED)
        source = case_source(context=context)
        scope = make_scope(context=context, with_upstream=True)
        store = _GenerationStore()

        class _SwitchingIndex(_Index):
            async def search_candidates(self, query):
                store.active = _generation().model_copy(
                    update={
                        "generation_id": "87000000-0000-4000-8000-000000000099",
                        "profile": EmbeddingProfile(
                            provider="test-provider",
                            model="embedding-4",
                            output_dimensions=4,
                            max_input_tokens=512,
                            semantic_min_similarity=0.8,
                        ),
                    }
                )
                return await super().search_candidates(query)

        index = _SwitchingIndex(source)
        session = RAGSearchService(
            generations=GenerationService(store),
            index=index,
            source_loader=_SourceLoader(source),
            embedding=None,
        ).bind(scope)

        records = await session.search(
            _intent(scope, HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW),
            operation_id="profile-switch",
        )

        assert records[0].case_ref.generation_id == GENERATION_ID
        assert index.queries[0].profile_id == _profile().profile_id
        assert store.active.generation_id.endswith("0099")

    asyncio.run(exercise())
