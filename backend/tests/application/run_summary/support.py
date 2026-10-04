from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.application.analysis_runs.models import SessionResultReference
from app.application.analysis_runs.topology_result_models import StableSourceReference
from app.application.canonical_json import canonical_json_digest
from app.application.reasoning.model_models import (
    StructuredOutputCompleted,
    Terminal,
    UsageReported,
    VisibleTextDelta,
    WaterbodyFinishReason,
    WaterbodyModelEvent,
)
from app.application.run_summary.models import (
    NotableFinding,
    ReportWindowSummary,
    RunSummaryInput,
    RunSummaryLimitFact,
    TargetResultProjection,
    WaterbodyReportCard,
)
from app.domain import ModelExecutionSummary, ModelSelection, TimeRange


RUN_ID = UUID("15a64a52-c1e2-4725-88ee-6c83f105e624")
SUMMARY_SESSION_ID = UUID("35a64a52-c1e2-4725-88ee-6c83f105e626")
TARGET_SESSION_ID = UUID("45a64a52-c1e2-4725-88ee-6c83f105e627")
UPSTREAM_SESSION_ID = UUID("55a64a52-c1e2-4725-88ee-6c83f105e628")
TARGET_ITEM_ID = UUID("65a64a52-c1e2-4725-88ee-6c83f105e629")
TOPOLOGY_ITEM_ID = UUID("75a64a52-c1e2-4725-88ee-6c83f105e630")
INPUT_ITEM_ID = UUID("85a64a52-c1e2-4725-88ee-6c83f105e631")
NOW = datetime(2026, 10, 3, 2, tzinfo=timezone.utc)
MODEL = ModelSelection(provider="openai", model="gpt-test")
EXECUTION = ModelExecutionSummary(
    model=MODEL,
    planning_calls=1,
    tool_calls=0,
    reasoning_batches=1,
    repair_calls=0,
    synthesis_calls=1,
    skipped_cells=0,
)
TARGET_SOURCE = StableSourceReference(
    run_id=RUN_ID,
    session_id=TARGET_SESSION_ID,
    item_id=TARGET_ITEM_ID,
    content_type="inference_result",
    content_schema_version=1,
    result_id="result:300",
    cell_id="cell:300:00010:w1",
    window_id="w1",
)
TOPOLOGY_SOURCE = StableSourceReference(
    run_id=RUN_ID,
    item_id=TOPOLOGY_ITEM_ID,
    content_type="topology_ready",
    content_schema_version=1,
)


def summary_input(*, query: str = "Summarize COMID 300.") -> RunSummaryInput:
    target = TargetResultProjection(
        target_comid="300",
        session_id=TARGET_SESSION_ID,
        graph_position=1,
        direct_upstream_comids=("100",),
        result_ref=SessionResultReference(run_id=RUN_ID, item_id=TARGET_ITEM_ID),
        mode="prediction",
        overall_risk="medium",
        availability="partial",
        confidence=0.8,
        key_reasons=("The target has a supported medium-risk signal.",),
        window_summaries=(
            ReportWindowSummary(
                window_id="w1",
                overall_risk="medium",
                availability="partial",
                confidence=0.8,
                source_refs=(TARGET_SOURCE,),
            ),
        ),
        notable_findings=(
            NotableFinding(
                kind="notable_value",
                text="The cited target value is notable.",
                source_ref=TARGET_SOURCE,
                display_value="7",
                display_unit="mg/L",
                window_id="w1",
            ),
        ),
        model_execution_summary=EXECUTION,
        time_range=TimeRange(start=NOW, end=NOW + timedelta(days=1)),
        analysis_timezone="UTC",
    )
    card = WaterbodyReportCard(
        target_comid="100",
        session_id=UPSTREAM_SESSION_ID,
        graph_position=0,
        dependency_status="invalid",
    )
    limit = RunSummaryLimitFact(
        code="partial_topology_coverage",
        text="One upstream branch did not provide a valid result.",
        source_ref=TOPOLOGY_SOURCE,
    )
    values = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "session_id": SUMMARY_SESSION_ID,
        "query": query,
        "target_comid": "300",
        "mode": "prediction",
        "time_range": target.time_range,
        "analysis_timezone": "UTC",
        "graph_coverage": "partial",
        "limit_facts": (limit,),
        "target_result_projection": target,
        "report_cards": (card,),
        "allowed_source_refs": (TOPOLOGY_SOURCE, TARGET_SOURCE),
    }
    return RunSummaryInput(
        **values,
        input_digest=canonical_json_digest(values),
    )


def draft_payload(
    *,
    source_ref: StableSourceReference = TARGET_SOURCE,
    summary: str = "COMID 300 has a medium-risk target result with partial coverage.",
    language: str = "en",
    highlights: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "language": language,
        "target_comid": "300",
        "graph_coverage": "partial",
        "covered_comids": ["100", "300"],
        "plain_language_summary": summary,
        "highlights": highlights
        if highlights is not None
        else [
            {
                "kind": "notable_value",
                "text": "COMID 300 has the cited value 7 mg/L in w1.",
                "source_ref": source_ref.model_dump(mode="json"),
                "display_value": "7",
                "display_unit": "mg/L",
                "window_id": "w1",
            }
        ],
    }


class FakeSummaryModelPort:
    def __init__(self, outputs: list[object]) -> None:
        self.outputs = outputs
        self.requests = []

    async def stream(self, request) -> AsyncIterator[WaterbodyModelEvent]:
        self.requests.append(request)
        output = self.outputs[len(self.requests) - 1]
        if isinstance(output, Exception):
            raise output
        visible_text = output if isinstance(output, str) else "A plain-language summary."
        yield VisibleTextDelta(
            operation_id=request.operation_id,
            model=request.model,
            message_id=f"message-{len(self.requests)}",
            text=visible_text,
        )
        if not isinstance(output, str):
            yield StructuredOutputCompleted(
                operation_id=request.operation_id,
                model=request.model,
                output_id=f"output-{len(self.requests)}",
                value=json.dumps(output),
            )
        yield UsageReported(
            operation_id=request.operation_id,
            model=request.model,
            input_tokens=10,
            output_tokens=5,
        )
        yield Terminal(
            operation_id=request.operation_id,
            model=request.model,
            provider_response_id=f"response-{len(self.requests)}",
            finish_reason=WaterbodyFinishReason.COMPLETED,
        )
