from dataclasses import replace

import pytest

from app.application.analysis_runs.models import AgentSessionStatus, RunStatus
from app.application.transcript_memory import (
    FinalEligibilityContext,
    FinalEligibilityReason,
    evaluate_final_fragment_eligibility,
    evaluate_final_result_eligibility,
)
from app.domain import CollectionStatus, EvidenceQualityClass
from tests.application.transcript_memory.m3_support import case_source


DENIED_CONTENT_TYPES = (
    "analysis_request",
    "lifecycle_event",
    "error",
    "runtime_fact",
    "query_parser_input",
    "query_parser_message",
    "query_understood",
    "topology_ready",
    "evidence_tool_call",
    "evidence_source_records",
    "evidence_tool_result",
    "evidence_bundle",
    "waterbody_agent_input",
    "waterbody_model_request",
    "waterbody_agent_message",
    "waterbody_model_response",
    "reasoning_decision",
    "waterbody_dependency_manifest",
    "run_summary_input",
    "run_summary_model_request",
    "run_summary_agent_message",
    "run_summary_model_response",
    "run_summary_result",
    "topology_run_result",
    "unknown_content",
)


def _context(source, item=None, **changes) -> FinalEligibilityContext:
    values = {
        "run_status": source.run_status,
        "session": source.session,
        "persistence_confirmed": source.persistence_confirmed,
        "source": item or source.result,
        "confirmed_references": source.confirmed_references,
    }
    values.update(changes)
    return FinalEligibilityContext(**values)


def test_final_eligibility_allows_only_valid_result_and_direct_fragment() -> None:
    source = case_source()

    result_decision = evaluate_final_result_eligibility(_context(source))
    fragment_decision = evaluate_final_fragment_eligibility(
        _context(source, source.fragments[0]),
        root=source.result,
    )

    assert result_decision.eligible
    assert result_decision.reason is FinalEligibilityReason.ELIGIBLE
    assert fragment_decision.eligible
    assert fragment_decision.reason is FinalEligibilityReason.ELIGIBLE


@pytest.mark.parametrize("content_type", DENIED_CONTENT_TYPES)
def test_result_allowlist_rejects_every_non_result_content_type(
    content_type: str,
) -> None:
    source = case_source()
    denied = replace(
        source.result,
        item=source.result.item.model_copy(update={"content_type": content_type}),
    )

    decision = evaluate_final_result_eligibility(_context(source, denied))

    assert not decision.eligible
    assert decision.reason is FinalEligibilityReason.CONTENT_NOT_ALLOWED


@pytest.mark.parametrize(
    ("changes", "expected"),
    (
        ({"run_status": RunStatus.RUNNING}, FinalEligibilityReason.LIFECYCLE_NOT_COMPLETED),
        ({"run_status": RunStatus.CANCELLED}, FinalEligibilityReason.LIFECYCLE_NOT_COMPLETED),
        ({"run_status": RunStatus.FAILED}, FinalEligibilityReason.LIFECYCLE_NOT_COMPLETED),
        ({"persistence_confirmed": False}, FinalEligibilityReason.PERSISTENCE_UNCONFIRMED),
        ({"confirmed_references": ()}, FinalEligibilityReason.REFERENCE_CLOSURE_INVALID),
    ),
)
def test_result_common_eligibility_fails_closed(changes, expected) -> None:
    source = case_source()

    decision = evaluate_final_result_eligibility(_context(source, **changes))

    assert not decision.eligible
    assert decision.reason is expected


@pytest.mark.parametrize(
    "session_status",
    (
        AgentSessionStatus.PENDING,
        AgentSessionStatus.RUNNING,
        AgentSessionStatus.FAILED,
        AgentSessionStatus.CANCELLED,
    ),
)
def test_result_requires_completed_waterbody_session(session_status) -> None:
    source = case_source()
    session = source.session.model_copy(update={"status": session_status})

    decision = evaluate_final_result_eligibility(
        _context(source, session=session)
    )

    assert not decision.eligible
    assert decision.reason is FinalEligibilityReason.LIFECYCLE_NOT_COMPLETED


def test_result_rejects_candidate_schema_and_ownership_mismatches() -> None:
    source = case_source()
    not_candidate = replace(
        source.result,
        item=source.result.item.model_copy(update={"rag_eligible": False}),
    )
    wrong_schema = replace(
        source.result,
        item=source.result.item.model_copy(update={"content_schema_version": 2}),
    )
    wrong_owner = replace(
        source.result,
        body=source.result.body.model_copy(update={"target_comid": "200"}),
    )

    assert evaluate_final_result_eligibility(
        _context(source, not_candidate)
    ).reason is FinalEligibilityReason.CANDIDATE_NOT_MARKED
    assert evaluate_final_result_eligibility(
        _context(source, wrong_schema)
    ).reason is FinalEligibilityReason.CONTENT_NOT_ALLOWED
    assert evaluate_final_result_eligibility(
        _context(source, wrong_owner)
    ).reason is FinalEligibilityReason.OWNERSHIP_MISMATCH


@pytest.mark.parametrize(
    ("fragment_changes", "item_changes", "expected"),
    (
        (
            {"rag_eligible": False},
            {},
            FinalEligibilityReason.FRAGMENT_POLICY_REJECTED,
        ),
        (
            {"collection_status": CollectionStatus.UNAVAILABLE},
            {},
            FinalEligibilityReason.FRAGMENT_POLICY_REJECTED,
        ),
        (
            {"quality_class": EvidenceQualityClass.INVALID},
            {},
            FinalEligibilityReason.FRAGMENT_POLICY_REJECTED,
        ),
        ({}, {"rag_eligible": False}, FinalEligibilityReason.CANDIDATE_NOT_MARKED),
        (
            {},
            {"content_schema_version": 2},
            FinalEligibilityReason.CONTENT_NOT_ALLOWED,
        ),
    ),
)
def test_fragment_policy_rejects_non_eligible_variants(
    fragment_changes,
    item_changes,
    expected,
) -> None:
    source = case_source()
    original = source.fragments[0]
    candidate = replace(
        original,
        item=original.item.model_copy(update=item_changes),
        body=original.body.model_copy(update=fragment_changes),
    )

    decision = evaluate_final_fragment_eligibility(
        _context(source, candidate),
        root=source.result,
    )

    assert not decision.eligible
    assert decision.reason is expected


def test_fragment_must_be_owned_and_directly_referenced_by_result() -> None:
    source = case_source()
    fragment = source.fragments[0]
    wrong_session = replace(
        fragment,
        item=fragment.item.model_copy(update={"session_id": None}),
    )
    run_global_confirmed = tuple(
        replace(reference, session_id=None)
        for reference in source.confirmed_references
    )
    root_without_fragment_ref = replace(source.result, references=())

    ownership = evaluate_final_fragment_eligibility(
        _context(
            source,
            wrong_session,
            confirmed_references=run_global_confirmed,
        ),
        root=source.result,
    )
    direct = evaluate_final_fragment_eligibility(
        _context(source, fragment),
        root=root_without_fragment_ref,
    )

    assert ownership.reason is FinalEligibilityReason.OWNERSHIP_MISMATCH
    assert direct.reason is FinalEligibilityReason.FRAGMENT_NOT_DIRECTLY_ASSOCIATED
