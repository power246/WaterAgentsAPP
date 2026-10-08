import asyncio
import inspect

import pytest

import app.domain as domain
from app.application.evidence_tools import (
    EvidenceProducerStatus,
    EvidenceScopeViolationError,
)
from app.domain import (
    EvidenceFragment,
    EvidenceToolOptions,
    HistoricalInferencePayload,
    HistoricalTranscriptRecord,
    MemoryMode,
    TranscriptEvidenceRequest,
)
from app.ports import RAGIndexPort
from tests.application.evidence_tools.fakes import (
    FakeCollectionPort,
    MemoryEvidenceStore,
    complete_batch,
)
from tests.application.evidence_tools.conftest import make_context, make_scope
from tests.application.evidence_tools.test_executor import executor, request
from tests.domain.test_evidence import sample_fragments


def test_historical_search_strategy_is_the_closed_five_value_contract() -> None:
    strategy_type = getattr(domain, "HistoricalSearchStrategy", None) or getattr(
        domain,
        "RAGSearchStrategy",
        None,
    )

    assert strategy_type is not None, "M8 V1 strategy enum is not implemented"
    assert tuple(strategy.value for strategy in strategy_type) == (
        "exact_comid_parameter_window",
        "nearby_comid_parameter",
        "parameter_season",
        "semantic_case_query",
        "topology_neighborhood_parameter",
    )


def test_transcript_request_replaces_free_query_with_typed_query_intent() -> None:
    fields = TranscriptEvidenceRequest.model_fields

    assert "query" not in fields, "raw query remains in the transcript search contract"
    assert "intent" in fields or {
        "strategy",
        "parameter_keys",
        "window_ids",
        "limit",
    } <= set(fields)


def test_historical_case_ref_has_the_complete_multi_waterbody_identity() -> None:
    case_ref = getattr(domain, "HistoricalCaseRef", None)

    assert case_ref is not None, "complete historical case identity is not implemented"
    fields = set(case_ref.model_fields)
    assert {
        "target_comid",
        "analysis_context_item_id",
        "result_item_id",
    } <= fields
    assert {"run_id", "session_id"} <= fields or {
        "original_run_id",
        "original_session_id",
    } <= fields


def test_historical_record_uses_typed_match_and_bounded_result_projection() -> None:
    fields = HistoricalTranscriptRecord.model_fields

    assert {"case_ref", "result_projection", "supporting_fragments"} <= set(fields)
    assert fields["matched_by"].annotation is not str


def test_historical_payload_is_typed_contextual_only_and_not_recursive_memory() -> None:
    fields = HistoricalInferencePayload.model_fields

    assert "projection" not in fields, "historical payload remains an opaque string"
    assert {
        "case_ref",
        "historical_window_id",
        "cell_projection",
        "supporting_fragment_refs",
    } <= set(fields)


def test_historical_tool_options_expose_a_typed_strategy() -> None:
    properties = EvidenceToolOptions.model_json_schema()["properties"]

    assert "historical_search_strategy" in properties


def test_materialized_historical_wrapper_cannot_become_recursive_memory() -> None:
    historical = sample_fragments()[-1]
    body = historical.model_dump(mode="python")
    body["rag_eligible"] = True

    with pytest.raises(ValueError, match="historical.*RAG eligible"):
        EvidenceFragment.model_validate(body)


def test_rag_index_port_exposes_generation_scoped_typed_operations() -> None:
    methods = {
        name
        for name, member in inspect.getmembers(RAGIndexPort, inspect.isfunction)
        if not name.startswith("_")
    }

    assert {
        "write_ready_projection",
        "search_candidates",
        "read_active_generation",
        "create_building_generation",
        "activate_generation",
        "retire_generation",
        "delete_generation",
    } <= methods
    assert not {"upsert", "search"} & methods


def test_invalid_historical_limit_is_zero_search_zero_prepare_zero_audit() -> None:
    async def exercise() -> None:
        scope = make_scope(
            context=make_context(memory_mode=MemoryMode.MEMORY_AUGMENTED)
        )
        store = MemoryEvidenceStore()
        producer = FakeCollectionPort(
            lambda provider_request: complete_batch(provider_request)
        )
        prepared_scopes = []
        runtime = executor(
            store,
            producer_statuses={
                "search_transcript_evidence": EvidenceProducerStatus.READY
            },
            transcript_port=producer,
            scope_preparer=prepared_scopes.append,
        )
        invalid_options = EvidenceToolOptions().model_copy(
            update={"record_limit": 6}
        )
        invalid_request = request(
            scope,
            "search_transcript_evidence",
            "call-m8-invalid-limit",
            options=invalid_options,
        )

        with pytest.raises(EvidenceScopeViolationError):
            await runtime.execute(scope, invalid_request)

        assert producer.calls == []
        assert prepared_scopes == []
        assert store.calls == []
        assert store.results == []

    asyncio.run(exercise())
