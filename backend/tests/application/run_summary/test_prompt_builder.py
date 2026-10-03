import asyncio
import json
from dataclasses import replace
from uuid import UUID

import pytest

from app.application.analysis_runs.topology_result_assembler import (
    TopologyResultAssembler,
    WaterbodyOutcomeAggregation,
)
from app.application.reasoning.batch_planner import (
    ModelFitCapability,
    ModelSizeEstimate,
)
from app.application.run_summary.errors import SummaryInputTooLargeError
from app.application.run_summary.input_builder import RunSummaryInputBuilder
from app.application.run_summary.prompt_builder import RunSummaryPromptBuilder
from app.application.transcripts.models import TranscriptContentRef
from tests.application.analysis_runs._topology_scheduler_support import topology
from tests.application.evidence_tools.test_upstream_result_store import (
    FakeResultReader,
    RUN_ID as FIXTURE_RUN_ID,
    dependency_outcome,
    inference_result,
    resolved_result,
)
from tests.application.run_summary.support import (
    INPUT_ITEM_ID,
    MODEL,
    NOW,
    RUN_ID,
    SUMMARY_SESSION_ID,
    summary_input,
)


class Estimator:
    def __init__(self, *, input_tokens: int = 100, output_tokens: int = 100) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

    def estimate(self, model, *, serialized_request, serialized_response_shape):
        assert model == MODEL
        assert serialized_request
        assert json.loads(serialized_response_shape)["summary_highlight_limit"] == 32
        return ModelSizeEstimate(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            estimated_output_bytes=1000,
        )


def test_prompt_contains_plain_language_fact_and_safety_policy() -> None:
    value = summary_input()
    request = RunSummaryPromptBuilder().build(
        value,
        model=MODEL,
        input_ref=TranscriptContentRef(run_id=RUN_ID, item_id=INPUT_ITEM_ID),
        operation_id=UUID("95a64a52-c1e2-4725-88ee-6c83f105e632"),
        deadline=NOW,
    )

    assert request.model == MODEL
    assert "target result is authoritative" in request.system_prompt
    assert "Never describe absent evidence" in request.system_prompt
    assert "Do not emit URLs" in request.system_prompt
    assert value.input_digest in request.messages[0].text
    assert request.messages[0].text.index('"report_cards"') < (
        request.messages[0].text.index('"target_result_projection"')
    )
    assert request.messages[0].text.rstrip().endswith("}")
    assert "tool_descriptors" not in type(request).model_fields
    schema = json.loads(request.response_schema.schema_text)
    assert schema["additionalProperties"] is False


def test_input_builder_uses_target_full_result_and_every_other_card_stably() -> None:
    async def exercise():
        graph = topology(("100", "300"), ("200", "300"))
        reader = FakeResultReader()
        outcomes = []
        for comid in ("100", "300"):
            session_id = UUID(f"30000000-0000-0000-0000-{int(comid):012d}")
            result = inference_result(comid, session_id)
            outcome = dependency_outcome(comid, session_id, result=result)
            reader.values[outcome.result_ref.item_id] = resolved_result(result)
            outcomes.append(outcome)
        outcomes.insert(
            1,
            dependency_outcome(
                "200",
                UUID("30000000-0000-0000-0000-000000000200"),
                status="invalid",
            ),
        )
        aggregation = TopologyResultAssembler(reader).aggregate(
            graph,
            tuple(reversed(outcomes)),
        )
        shuffled = replace(
            aggregation,
            outcomes=tuple(reversed(aggregation.outcomes)),
        )
        builder = RunSummaryInputBuilder(
            result_reader=reader,
            size_estimator=Estimator(),
        )
        arguments = dict(
            run_id=FIXTURE_RUN_ID,
            session_id=SUMMARY_SESSION_ID,
            query="Summarize the analysis.",
            topology=graph.model_copy(update={}),
            topology_ref=TranscriptContentRef(
                run_id=FIXTURE_RUN_ID,
                item_id=UUID("40000000-0000-0000-0000-000000000001"),
            ),
            aggregation=WaterbodyOutcomeAggregation(
                target_outcome=shuffled.target_outcome,
                outcomes=shuffled.outcomes,
                graph_coverage=shuffled.graph_coverage,
            ),
            model=MODEL,
            model_fit=ModelFitCapability(
                model=MODEL,
                context_window_tokens=100_000,
                max_output_tokens=2_000,
            ),
        )
        shuffled_value = await builder.build(**arguments)
        ordered_value = await builder.build(
            **{**arguments, "aggregation": aggregation}
        )
        return shuffled_value, ordered_value

    value, ordered = asyncio.run(exercise())
    assert tuple(card.target_comid for card in value.report_cards) == ("100", "200")
    assert value.target_result_projection.target_comid == "300"
    assert value.target_result_projection.full_result is not None
    assert value.target_result_projection.full_result.target_comid == "300"
    assert tuple(card.graph_position for card in value.report_cards) == (0, 1)
    assert value.target_result_projection.graph_position == 2
    assert value.input_digest == ordered.input_digest


def test_complete_input_that_does_not_fit_fails_without_dropping_nodes() -> None:
    value = summary_input()
    builder = RunSummaryInputBuilder.__new__(RunSummaryInputBuilder)
    builder._size_estimator = Estimator(input_tokens=10_000)
    from app.application.run_summary.input_builder import RunSummaryInputLimits
    builder._limits = RunSummaryInputLimits()

    with pytest.raises(SummaryInputTooLargeError):
        builder._require_fit(
            value,
            model=MODEL,
            model_fit=ModelFitCapability(
                model=MODEL,
                context_window_tokens=10_500,
                max_output_tokens=1_000,
            ),
        )
