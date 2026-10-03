from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.application.analysis_runs.models import SessionResultReference
from app.application.analysis_runs.topology_result_models import StableSourceReference
from app.application.canonical_json import canonical_json_digest
from app.application.run_summary.models import (
    NotableFinding,
    ReportWindowSummary,
    RunSummaryAgentMessage,
    RunSummaryHighlight,
    RunSummaryInput,
    RunSummaryJsonSchema,
    RunSummaryLimitFact,
    RunSummaryModelMessage,
    RunSummaryModelRequest,
    RunSummaryModelResponse,
    RunSummaryResult,
    TargetResultProjection,
    WaterbodyReportCard,
)
from app.application.transcripts.models import TranscriptContentRef
from app.domain.inference import ModelExecutionSummary
from app.domain.model_catalog import ModelSelection
from app.domain.time.time_range import TimeRange

RUN_ID = UUID("15a64a52-c1e2-4725-88ee-6c83f105e624")
OTHER_RUN_ID = UUID("25a64a52-c1e2-4725-88ee-6c83f105e625")
SUMMARY_SESSION_ID = UUID("35a64a52-c1e2-4725-88ee-6c83f105e626")
TARGET_SESSION_ID = UUID("45a64a52-c1e2-4725-88ee-6c83f105e627")
UPSTREAM_SESSION_ID = UUID("55a64a52-c1e2-4725-88ee-6c83f105e628")
NOW = datetime(2026, 10, 3, 2, tzinfo=timezone.utc)
MODEL = ModelSelection(provider="openai", model="gpt-test")
EXECUTION = ModelExecutionSummary(
    model=MODEL,
    planning_calls=1,
    tool_calls=1,
    reasoning_batches=1,
    repair_calls=0,
    synthesis_calls=1,
    skipped_cells=0,
)


def _source_ref(
    item_id: str,
    *,
    session_id: UUID | None,
    result_id: str | None = None,
    cell_id: str | None = None,
    window_id: str | None = None,
    content_type: str = "inference_result",
    run_id: UUID = RUN_ID,
) -> StableSourceReference:
    return StableSourceReference(
        run_id=run_id,
        session_id=session_id,
        item_id=UUID(item_id),
        content_type=content_type,
        content_schema_version=1,
        result_id=result_id,
        cell_id=cell_id,
        window_id=window_id,
    )


TARGET_SOURCE = _source_ref(
    "65a64a52-c1e2-4725-88ee-6c83f105e629",
    session_id=TARGET_SESSION_ID,
    result_id="result:300",
    cell_id="cell:300:00060:w1",
    window_id="w1",
)
UPSTREAM_SOURCE = _source_ref(
    "75a64a52-c1e2-4725-88ee-6c83f105e630",
    session_id=UPSTREAM_SESSION_ID,
    result_id="result:100",
    cell_id="cell:100:00060:w1",
    window_id="w1",
)
LIMIT_SOURCE = _source_ref(
    "85a64a52-c1e2-4725-88ee-6c83f105e631",
    session_id=None,
    content_type="topology_ready",
)


def _result_ref(item_id: str, *, run_id: UUID = RUN_ID) -> SessionResultReference:
    return SessionResultReference(run_id=run_id, item_id=UUID(item_id))


def _target() -> TargetResultProjection:
    return TargetResultProjection(
        target_comid="300",
        session_id=TARGET_SESSION_ID,
        graph_position=2,
        direct_upstream_comids=("200", "100"),
        result_ref=_result_ref("95a64a52-c1e2-4725-88ee-6c83f105e632"),
        mode="prediction",
        overall_risk="medium",
        availability="complete",
        confidence=0.8,
        key_reasons=("flow trend",),
        window_summaries=(
            ReportWindowSummary(
                window_id="w1",
                overall_risk="medium",
                availability="complete",
                confidence=0.8,
                source_refs=(TARGET_SOURCE,),
            ),
        ),
        model_execution_summary=EXECUTION,
        time_range=TimeRange(start=NOW, end=NOW + timedelta(days=1)),
        analysis_timezone="UTC",
    )


def _usable_card() -> WaterbodyReportCard:
    return WaterbodyReportCard(
        target_comid="100",
        session_id=UPSTREAM_SESSION_ID,
        graph_position=0,
        dependency_status="usable",
        result_ref=_result_ref("a5a64a52-c1e2-4725-88ee-6c83f105e633"),
        mode="prediction",
        overall_risk="low",
        availability="partial",
        confidence=0.6,
        notable_findings=(
            NotableFinding(
                kind="trend",
                text="Flow is increasing.",
                source_ref=UPSTREAM_SOURCE,
            ),
        ),
        model_execution_summary=EXECUTION,
    )


def _invalid_card() -> WaterbodyReportCard:
    return WaterbodyReportCard(
        target_comid="200",
        session_id=UUID("b5a64a52-c1e2-4725-88ee-6c83f105e634"),
        graph_position=1,
        direct_upstream_comids=("100",),
        dependency_status="invalid",
    )


def _summary_input(**changes: object) -> RunSummaryInput:
    values: dict[str, object] = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "session_id": SUMMARY_SESSION_ID,
        "query": "Summarize the topology analysis.",
        "target_comid": "300",
        "mode": "prediction",
        "time_range": TimeRange(start=NOW, end=NOW + timedelta(days=1)),
        "analysis_timezone": "UTC",
        "graph_coverage": "partial",
        "limit_facts": (
            RunSummaryLimitFact(
                code="distance_limit",
                text="The upstream search reached its distance limit.",
                source_ref=LIMIT_SOURCE,
            ),
        ),
        "target_result_projection": _target(),
        "report_cards": (_invalid_card(), _usable_card()),
        "allowed_source_refs": (UPSTREAM_SOURCE, LIMIT_SOURCE, TARGET_SOURCE),
    }
    values.update(changes)
    canonical_values = {
        **values,
        "report_cards": tuple(
            sorted(values["report_cards"], key=lambda item: item.graph_position)
        ),
        "allowed_source_refs": tuple(
            sorted(
                values["allowed_source_refs"],
                key=lambda item: (
                    str(item.run_id),
                    str(item.session_id or ""),
                    str(item.item_id),
                ),
            )
        ),
    }
    values["input_digest"] = canonical_json_digest(canonical_values)
    return RunSummaryInput.model_validate(values)


def _summary_result(source_ref: StableSourceReference = TARGET_SOURCE) -> RunSummaryResult:
    summary_input = _summary_input()
    return RunSummaryResult(
        run_id=RUN_ID,
        session_id=SUMMARY_SESSION_ID,
        input_digest=summary_input.input_digest,
        model=MODEL,
        language="en",
        plain_language_summary="The target has moderate risk.",
        highlights=(
            RunSummaryHighlight(
                kind="risk",
                text="The target risk is moderate.",
                source_ref=source_ref,
            ),
        ),
        generated_at=NOW,
    )


def test_summary_input_has_stable_card_and_reference_order_and_digest() -> None:
    summary_input = _summary_input()
    reordered = _summary_input(
        report_cards=tuple(reversed(summary_input.report_cards)),
        allowed_source_refs=tuple(reversed(summary_input.allowed_source_refs)),
    )

    assert tuple(card.target_comid for card in summary_input.report_cards) == (
        "100",
        "200",
    )
    assert reordered.input_digest == summary_input.input_digest


def test_target_and_cards_must_exactly_cover_graph_positions() -> None:
    with pytest.raises(ValidationError, match="must not be duplicated"):
        _summary_input(
            report_cards=(
                _usable_card().model_copy(
                    update={"target_comid": "300", "graph_position": 0}
                ),
                _invalid_card(),
            )
        )
    with pytest.raises(ValidationError, match="exactly cover"):
        _summary_input(report_cards=(_usable_card(),))
    with pytest.raises(ValidationError, match="final stable graph projection"):
        _summary_input(
            target_result_projection=_target().model_copy(update={"graph_position": 0}),
            report_cards=(
                _usable_card().model_copy(update={"graph_position": 1}),
                _invalid_card().model_copy(update={"graph_position": 2}),
            ),
        )


def test_invalid_card_cannot_claim_result_facts() -> None:
    with pytest.raises(ValidationError, match="cannot project result facts"):
        WaterbodyReportCard(
            target_comid="200",
            session_id=UPSTREAM_SESSION_ID,
            graph_position=1,
            dependency_status="invalid",
            key_reasons=("provider failed",),
        )


def test_summary_input_rejects_duplicate_missing_and_cross_run_refs() -> None:
    with pytest.raises(ValidationError, match="allowed source refs"):
        _summary_input(
            allowed_source_refs=(TARGET_SOURCE, TARGET_SOURCE, UPSTREAM_SOURCE, LIMIT_SOURCE)
        )
    with pytest.raises(ValidationError, match="exactly close"):
        _summary_input(allowed_source_refs=(TARGET_SOURCE, UPSTREAM_SOURCE))
    with pytest.raises(ValidationError, match="remain in the run"):
        _summary_input(
            limit_facts=(
                RunSummaryLimitFact(
                    code="distance_limit",
                    text="Search was limited.",
                    source_ref=LIMIT_SOURCE.model_copy(update={"run_id": OTHER_RUN_ID}),
                ),
            ),
            allowed_source_refs=(
                TARGET_SOURCE,
                UPSTREAM_SOURCE,
                LIMIT_SOURCE.model_copy(update={"run_id": OTHER_RUN_ID}),
            ),
        )


def test_summary_result_reference_closure_is_checked_against_input() -> None:
    summary_input = _summary_input()
    summary_input.validate_result(_summary_result())

    unknown = TARGET_SOURCE.model_copy(
        update={"item_id": UUID("c5a64a52-c1e2-4725-88ee-6c83f105e635")}
    )
    with pytest.raises(ValueError, match="outside its input"):
        summary_input.validate_result(_summary_result(unknown))
    with pytest.raises(ValueError, match="identity"):
        summary_input.validate_result(
            _summary_result().model_copy(update={"run_id": OTHER_RUN_ID})
        )


def test_highlight_numeric_display_fields_are_atomic_and_ref_bound() -> None:
    with pytest.raises(ValidationError, match="must appear together"):
        RunSummaryHighlight(
            kind="flow",
            text="Flow value.",
            source_ref=TARGET_SOURCE,
            display_value=12.5,
        )
    with pytest.raises(ValidationError, match="must match source_ref"):
        RunSummaryHighlight(
            kind="flow",
            text="Flow value.",
            source_ref=TARGET_SOURCE,
            display_value=12.5,
            display_unit="ft3/s",
            window_id="w2",
        )


def test_summary_model_payloads_are_provider_neutral_and_tool_free() -> None:
    input_ref = TranscriptContentRef(
        run_id=RUN_ID,
        item_id=UUID("d5a64a52-c1e2-4725-88ee-6c83f105e636"),
    )
    request_values: dict[str, object] = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "session_id": SUMMARY_SESSION_ID,
        "operation_id": UUID("e5a64a52-c1e2-4725-88ee-6c83f105e637"),
        "model": MODEL,
        "input_ref": input_ref,
        "input_digest": _summary_input().input_digest,
        "system_prompt": "Write a concise factual summary.",
        "messages": (
            RunSummaryModelMessage(message_id="summary-input", role="user", text="Input"),
        ),
        "response_schema": RunSummaryJsonSchema(
            name="run_summary",
            schema_text='{"type":"object"}',
        ),
        "deadline": NOW + timedelta(minutes=2),
    }
    request = RunSummaryModelRequest(
        **request_values,
        request_digest=canonical_json_digest(request_values),
    )
    message = RunSummaryAgentMessage(
        run_id=RUN_ID,
        session_id=SUMMARY_SESSION_ID,
        message_id="visible-summary",
        operation_id=request.operation_id,
        text="Done.",
        generation_status="completed",
        end_reason="completed",
    )
    response = RunSummaryModelResponse(
        run_id=RUN_ID,
        session_id=SUMMARY_SESSION_ID,
        operation_id=request.operation_id,
        model=MODEL,
        finish_reason="completed",
        message_refs=(input_ref,),
        structured_output='{"highlights":[]}',
        visible_text="Done.",
        provider_response_id="response-1",
    )

    assert message.text == response.visible_text
    assert "tool_descriptors" not in RunSummaryModelRequest.model_fields
    assert "tool_calls" not in RunSummaryModelResponse.model_fields
    with pytest.raises(ValidationError, match="Extra inputs"):
        RunSummaryModelRequest.model_validate(
            {**request.model_dump(), "tool_descriptors": []}
        )
