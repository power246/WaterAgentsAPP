import asyncio

import pytest

from app.application.evidence_tools import EvidenceProducerStatus
from app.application.evidence_tools.scope import with_evidence_rule_plan
from app.application.query_understanding import (
    EvidenceInferenceStrength,
    EvidenceRuleOrigin,
    EvidenceSpatialRelation,
    EvidenceTemporalRelation,
    append_evidence_rules,
    create_evidence_rule,
)
from app.application.transcript_memory import (
    RAGSearchInvalidError,
    RAGSearchUnavailableError,
    TranscriptEvidenceProducer,
)
from app.application.transcript_memory.search import RAGSearchSession
from app.domain import (
    CollectionStatus,
    EvidenceToolExecutionStatus,
    EvidenceToolOptions,
    HistoricalSearchIntentV1,
    HistoricalSearchStrategy,
    MemoryMode,
    TranscriptEvidenceRequest,
)
from tests.application.evidence_tools.fakes import (
    FakeCollectionPort,
    MemoryEvidenceStore,
    complete_batch,
)
from tests.application.evidence_tools.test_executor import executor, request
from tests.application.evidence_tools.conftest import make_context, make_scope


class FakeSearchSession:
    def __init__(self, outcome=()) -> None:
        self.outcome = outcome
        self.calls = []
        self.cleared = False
        self.rebound_scopes = []

    def rebind(self, scope):
        self.rebound_scopes.append(scope)
        return self

    async def search(self, intent, *, operation_id, deadline=None):
        self.calls.append((intent, operation_id, deadline))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    def clear(self) -> None:
        self.cleared = True


class FakeSearchService:
    def __init__(self, session) -> None:
        self.session = session
        self.bound = []

    def bind(self, scope):
        self.bound.append(scope)
        return self.session


def _intent(scope, strategy=HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW):
    return HistoricalSearchIntentV1(
        strategy=strategy,
        parameter_keys=(scope.requested_parameters[0],),
        window_ids=(scope.time_windows[0].window_id,),
        limit=5,
    )


def _provider_request(scope, *, strategy=None):
    return TranscriptEvidenceRequest(
        operation_id="operation-001",
        run_id=scope.run_id,
        session_id=scope.session_id,
        memory_mode=scope.memory_mode,
        intent=_intent(
            scope,
            strategy or HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW,
        ),
    )


def test_producer_binds_only_sealed_memory_scope_and_returns_completed_empty(
) -> None:
    async def exercise() -> None:
        scope = make_scope(
            context=make_context(memory_mode=MemoryMode.MEMORY_AUGMENTED)
        )
        session = FakeSearchSession()
        service = FakeSearchService(session)
        producer = TranscriptEvidenceProducer(service)

        producer.prepare_scope(scope)
        producer.prepare_scope(scope)
        batch = await producer.collect(_provider_request(scope), deadline=12.5)

        assert service.bound == [scope]
        assert len(session.calls) == 1
        assert batch.operation_id == "operation-001"
        assert batch.completion_proof.complete
        assert batch.records == ()
        assert batch.counts.model_dump() == {
            "requests": 1,
            "pages": 1,
            "records": 0,
            "response_bytes": 0,
        }
        producer.cleanup_run(scope.run_id)
        assert session.cleared

    asyncio.run(exercise())


def test_producer_propagates_only_append_only_rules_to_existing_search_session():
    scope = make_scope(context=make_context(memory_mode=MemoryMode.MEMORY_AUGMENTED))
    rule = create_evidence_rule(
        output_parameter=scope.requested_parameters[0],
        input_parameters=(scope.supporting_parameters[0],),
        temporal_relation=EvidenceTemporalRelation.SAME_WINDOW,
        spatial_relation=EvidenceSpatialRelation.SAME_WATERBODY,
        maximum_inference_strength=EvidenceInferenceStrength.DIRECTIONAL,
        required_collection_attempt=False,
        rationale="Discharge supports the target directionally.",
        origin=EvidenceRuleOrigin.WATERBODY_AGENT,
        origin_session_id=scope.session_id,
    )
    updated = with_evidence_rule_plan(
        scope, append_evidence_rules(scope.evidence_rule_plan, (rule,))
    )
    session = FakeSearchSession()
    service = FakeSearchService(session)
    producer = TranscriptEvidenceProducer(service)

    producer.prepare_scope(scope)
    producer.prepare_scope(updated)
    producer.prepare_scope(updated)

    assert service.bound == [scope]
    assert session.rebound_scopes == [updated]
    with pytest.raises(ValueError, match="extend"):
        producer.prepare_scope(scope)

    embedding = object()
    bound = RAGSearchSession(service=object(), scope=scope, embedding=embedding)
    bound._embedding_cache[("profile", "query")] = object()
    rebound = bound.rebind(updated)
    assert rebound._scope == updated
    assert bound._scope == scope
    assert rebound._embedding is embedding
    assert rebound._embedding_cache is bound._embedding_cache


@pytest.mark.parametrize(
    ("error", "safe_code"),
    (
        (RAGSearchUnavailableError("index_building"), "index_building"),
        (RAGSearchUnavailableError("embedding_unavailable"), "embedding_unavailable"),
        (RAGSearchInvalidError("embedding_protocol_invalid"), "embedding_protocol_invalid"),
        (RAGSearchInvalidError("historical_source_digest_invalid"), "historical_source_invalid"),
        (RAGSearchInvalidError("historical_index_generation_invalid"), "index_invalid"),
    ),
)
def test_producer_maps_search_failures_to_closed_typed_batches(
    error,
    safe_code,
) -> None:
    async def exercise() -> None:
        scope = make_scope(
            context=make_context(memory_mode=MemoryMode.MEMORY_AUGMENTED)
        )
        producer = TranscriptEvidenceProducer(
            FakeSearchService(FakeSearchSession(error))
        )
        producer.prepare_scope(scope)

        batch = await producer.collect(_provider_request(scope))

        assert not batch.completion_proof.complete
        assert batch.incomplete_reason == safe_code
        assert batch.records == ()
        assert batch.counts.records == 0

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("error", "safe_code", "status"),
    (
        (
            RAGSearchUnavailableError("index_building"),
            "index_building",
            CollectionStatus.UNAVAILABLE,
        ),
        (
            RAGSearchUnavailableError("embedding_unavailable"),
            "embedding_unavailable",
            CollectionStatus.UNAVAILABLE,
        ),
        (
            RAGSearchInvalidError("historical_source_invalid"),
            "historical_source_invalid",
            CollectionStatus.INVALID,
        ),
    ),
)
def test_m5_round_trip_preserves_historical_failure_kind(
    error,
    safe_code,
    status,
) -> None:
    async def exercise() -> None:
        scope = make_scope(
            context=make_context(memory_mode=MemoryMode.MEMORY_AUGMENTED)
        )
        producer = TranscriptEvidenceProducer(
            FakeSearchService(FakeSearchSession(error))
        )
        store = MemoryEvidenceStore()
        runtime = executor(
            store,
            producer_statuses={
                "search_transcript_evidence": EvidenceProducerStatus.READY
            },
            transcript_port=producer,
            scope_preparer=producer.prepare_scope,
        )
        result = await runtime.execute(
            scope,
            request(
                scope,
                "search_transcript_evidence",
                f"call-{safe_code}",
                options=EvidenceToolOptions(
                    historical_search_strategy=(
                        HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW
                    ),
                    record_limit=5,
                ),
            ),
        )

        assert result.execution_status is EvidenceToolExecutionStatus.FAILED
        assert result.collection_status is status
        assert result.safe_error.code == safe_code
        assert result.source_record_refs == ()

    asyncio.run(exercise())


def test_historical_tool_budget_allows_two_server_enforced_calls_per_session(
) -> None:
    async def exercise() -> None:
        scope = make_scope(
            context=make_context(memory_mode=MemoryMode.MEMORY_AUGMENTED)
        )
        store = MemoryEvidenceStore()
        port = FakeCollectionPort(lambda item: complete_batch(item))
        runtime = executor(
            store,
            producer_statuses={
                "search_transcript_evidence": EvidenceProducerStatus.READY
            },
            transcript_port=port,
        )

        results = []
        for index, strategy in enumerate(HistoricalSearchStrategy, start=1):
            results.append(
                await runtime.execute(
                    scope,
                    request(
                        scope,
                        "search_transcript_evidence",
                        f"call-{index}",
                        options=EvidenceToolOptions(
                            historical_search_strategy=strategy,
                            record_limit=5,
                        ),
                    ),
                )
            )
            if index == 3:
                break

        assert [
            item.execution_status for item in results
        ] == [
            EvidenceToolExecutionStatus.COMPLETED,
            EvidenceToolExecutionStatus.COMPLETED,
            EvidenceToolExecutionStatus.FAILED,
        ]
        assert results[2].safe_error.code == "evidence_tool_budget_exceeded"
        assert len(port.calls) == 2

        second_scope = make_scope(
            run_id=scope.run_id,
            session_id="session-2",
            context=make_context(memory_mode=MemoryMode.MEMORY_AUGMENTED),
        )
        independent = await runtime.execute(
            second_scope,
            request(
                second_scope,
                "search_transcript_evidence",
                "call-independent",
                options=EvidenceToolOptions(
                    historical_search_strategy=(
                        HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW
                    ),
                    record_limit=5,
                ),
            ),
        )
        assert independent.execution_status is EvidenceToolExecutionStatus.COMPLETED
        assert len(port.calls) == 3

    asyncio.run(exercise())
