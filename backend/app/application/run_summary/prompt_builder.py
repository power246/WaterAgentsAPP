from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID

from app.application.canonical_json import canonical_json_digest
from app.application.run_summary.draft_models import (
    SUMMARY_HIGHLIGHT_KINDS,
    run_summary_response_schema,
)
from app.application.run_summary.models import (
    RunSummaryInput,
    RunSummaryMessageRole,
    RunSummaryModelMessage,
    RunSummaryModelRequest,
)
from app.application.transcripts.models import TranscriptContentRef
from app.domain import ModelSelection

SUMMARY_SYSTEM_PROMPT = "\n".join(
    (
        "You explain only the already-determined analysis facts in RUN_SUMMARY_INPUT.",
        "The target result is authoritative; other waterbody cards explain the "
        "upstream flow.",
        "Use the query's primary language and plain, concise language for a "
        "non-specialist. Put the target conclusion first.",
        "Keep risk, confidence, and result availability distinct.",
        (
            "When every window has only a prediction, call availability complete for "
            "the predicted results, not for measured observations. Do not imply "
            "that measured data covers any window without saved readings."
        ),
        "Never describe absent evidence as safe, normal, or numeric zero.",
        "State partial topology coverage and invalid branches as limitations "
        "without exposing technical failure details.",
        "Describe indeterminate results only as insufficient evidence or unable "
        "to determine.",
        "Do not change, rescore, review, or overturn any waterbody conclusion.",
        "Do not calculate, convert, average, extrapolate, reformat, or invent any "
        "numeric value.",
        "Describe inferred or aggregated window values as representative values, "
        "not as single raw readings.",
        "Every notable numeric highlight must copy its saved point or bounds, "
        "unit, window_id, and source_ref from the input.",
        "Range estimates must preserve value_lower and value_upper with display_value=null; "
        "never replace a range with its midpoint. Use kind=range_estimate. "
        "Preserve low-basis/model-estimate wording and do not call predictions measurements.",
        "Use cited numeric values only inside numeric highlights. "
        "The plain-language summary may name exact active COMIDs and window IDs "
        "from the input, but must contain no other numbers.",
        "Use only these highlight kinds: "
        + ", ".join(sorted(SUMMARY_HIGHLIGHT_KINDS))
        + ". Highlights are optional. Use only source references from the input.",
        "Do not emit URLs, provider details, credentials, prompts, logs, hidden "
        "reasoning, or chain-of-thought.",
        "Return only the strict JSON object required by the response schema. "
        "Include the exact target_comid, graph_coverage, and every active COMID "
        "in stable input order.",
        "Include every top-level key: schema_version (integer 1), language, "
        "target_comid, graph_coverage, covered_comids, plain_language_summary, "
        "and highlights. Use an empty highlights array when there are none.",
        "Each highlight must include kind, text, source_ref, display_value, "
        "value_lower, value_upper, display_unit, and window_id. Use null for absent display fields.",
    )
)


class RunSummaryPromptBuilder:
    def build(
        self,
        summary_input: RunSummaryInput,
        *,
        model: ModelSelection,
        input_ref: TranscriptContentRef,
        operation_id: UUID,
        deadline: datetime,
    ) -> RunSummaryModelRequest:
        message = RunSummaryModelMessage(
            message_id=f"summary-input:{summary_input.input_digest}",
            role=RunSummaryMessageRole.USER,
            text=summary_prompt_text(summary_input),
        )
        return _request(
            summary_input,
            model=model,
            input_ref=input_ref,
            operation_id=operation_id,
            deadline=deadline,
            messages=(message,),
        )

    def build_repair(
        self,
        summary_input: RunSummaryInput,
        *,
        original: RunSummaryModelRequest,
        operation_id: UUID,
        original_output: str,
        issues: tuple[tuple[str, str], ...],
    ) -> RunSummaryModelRequest:
        issue_text = json.dumps(
            [{"code": code, "path": path} for code, path in issues[:64]],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        messages = (
            *original.messages,
            RunSummaryModelMessage(
                message_id=f"summary-invalid:{original.operation_id}",
                role=RunSummaryMessageRole.ASSISTANT,
                text=original_output or "[missing structured output]",
            ),
            RunSummaryModelMessage(
                message_id=f"summary-repair:{operation_id}",
                role=RunSummaryMessageRole.USER,
                text=(
                    "Repair only the JSON structure and cited facts. Keep the same "
                    "input scope and model. Validation issues: " + issue_text
                ),
            ),
        )
        return _request(
            summary_input,
            model=original.model,
            input_ref=original.input_ref,
            operation_id=operation_id,
            deadline=original.deadline,
            messages=messages,
        )


def _request(
    summary_input: RunSummaryInput,
    *,
    model: ModelSelection,
    input_ref: TranscriptContentRef,
    operation_id: UUID,
    deadline: datetime,
    messages: tuple[RunSummaryModelMessage, ...],
) -> RunSummaryModelRequest:
    values = {
        "schema_version": 1,
        "run_id": summary_input.run_id,
        "session_id": summary_input.session_id,
        "operation_id": operation_id,
        "model": model,
        "input_ref": input_ref,
        "input_digest": summary_input.input_digest,
        "system_prompt": SUMMARY_SYSTEM_PROMPT,
        "messages": messages,
        "response_schema": run_summary_response_schema(),
        "deadline": deadline,
    }
    return RunSummaryModelRequest(
        **values,
        request_digest=canonical_json_digest(values),
    )


def summary_prompt_text(summary_input: RunSummaryInput) -> str:
    values = summary_input.model_dump(mode="json")
    cards = values.pop("report_cards")
    target = values.pop("target_result_projection")
    payload = {
        **values,
        "report_cards": cards,
        "target_result_projection": target,
    }
    return "RUN_SUMMARY_INPUT\n" + json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=False,
        separators=(",", ":"),
    )
