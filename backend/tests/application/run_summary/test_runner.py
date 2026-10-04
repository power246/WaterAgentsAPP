import asyncio
import json
from dataclasses import replace
from uuid import UUID

import pytest

from app.application.analysis_runs.topology_result_assembler import (
    TopologyResultAssembler,
)
from app.application.reasoning.batch_planner import ModelFitCapability
from app.application.run_summary.stage import RunSummaryStage
from app.application.run_summary.errors import SummaryInputTooLargeError
from app.application.run_summary.runner import (
    RunSummaryRunStatus,
    RunSummaryRunner,
)
from app.application.transcripts.models import TranscriptContentRef
from tests.application.run_summary.support import (
    INPUT_ITEM_ID,
    MODEL,
    NOW,
    RUN_ID,
    SUMMARY_SESSION_ID,
    TARGET_SOURCE,
    FakeSummaryModelPort,
    draft_payload,
    summary_input,
)
from tests.application.analysis_runs._topology_scheduler_support import (
    outcome as node_outcome,
    topology,
)
from tests.application.evidence_tools.test_upstream_result_store import (
    FakeResultReader,
    RUN_ID as FIXTURE_RUN_ID,
)


def run_runner(outputs):
    port = FakeSummaryModelPort(outputs)
    runner = RunSummaryRunner(
        model_port=port,
        operation_id_factory=lambda: UUID(
            "95a64a52-c1e2-4725-88ee-6c83f105e632"
        ),
    )
    outcome = asyncio.run(
        runner.run(
            summary_input(),
            model=MODEL,
            deadline=NOW,
            input_ref=TranscriptContentRef(run_id=RUN_ID, item_id=INPUT_ITEM_ID),
        )
    )
    return outcome, port


def test_runner_uses_exact_model_no_tools_and_one_main_call() -> None:
    outcome, port = run_runner([draft_payload()])

    assert outcome.status is RunSummaryRunStatus.COMPLETED
    assert outcome.model_calls == 1
    assert outcome.repaired is False
    assert outcome.result is not None
    assert outcome.result.model == MODEL
    assert outcome.result.usage.input_tokens == 10
    assert len(port.requests) == 1
    assert port.requests[0].model == MODEL
    assert "tool_descriptors" not in type(port.requests[0]).model_fields


def test_invalid_numeric_shape_repairs_once_with_same_model_and_scope() -> None:
    invalid = draft_payload()
    del invalid["highlights"][0]["display_unit"]
    outcome, port = run_runner([invalid, draft_payload()])

    assert outcome.status is RunSummaryRunStatus.COMPLETED
    assert outcome.model_calls == 2
    assert outcome.repaired is True
    assert outcome.result.usage.input_tokens == 20
    initial, repair = port.requests
    assert repair.model == initial.model == MODEL
    assert repair.input_ref == initial.input_ref
    assert repair.input_digest == initial.input_digest
    assert repair.response_schema == initial.response_schema
    assert repair.system_prompt == initial.system_prompt


def test_bad_ref_or_url_after_the_only_repair_fails_without_fallback() -> None:
    bad_ref = TARGET_SOURCE.model_copy(
        update={"item_id": UUID("a5a64a52-c1e2-4725-88ee-6c83f105e633")}
    )
    for invalid in (
        draft_payload(source_ref=bad_ref),
        draft_payload(summary="See https://agent.test for COMID 300."),
    ):
        outcome, port = run_runner([invalid, invalid])
        assert outcome.status is RunSummaryRunStatus.FAILED
        assert outcome.error.code == "run_summary_invalid"
        assert outcome.model_calls == 2
        assert outcome.result is None
        assert len(port.requests) == 2


def test_visible_only_text_is_a_protocol_failure_without_repair() -> None:
    outcome, port = run_runner([json.dumps(draft_payload())])

    assert outcome.status is RunSummaryRunStatus.FAILED
    assert outcome.error.code == "run_summary_failed"
    assert outcome.error.retryable is False
    assert outcome.result is None
    assert outcome.model_calls == 1
    assert len(port.requests) == 1


def test_cancel_after_validated_model_output_suppresses_summary_result() -> None:
    class CancelAtCommit:
        def __init__(self) -> None:
            self.checks = 0

        def is_requested(self):
            self.checks += 1
            return self.checks >= 7

    port = FakeSummaryModelPort([draft_payload()])
    outcome = asyncio.run(RunSummaryRunner(model_port=port).run(
        summary_input(),
        model=MODEL,
        deadline=NOW,
        input_ref=TranscriptContentRef(run_id=RUN_ID, item_id=INPUT_ITEM_ID),
        cancellation=CancelAtCommit(),
    ))

    assert outcome.status is RunSummaryRunStatus.CANCELLED
    assert outcome.result is None
    assert outcome.model_calls == 1


class _NotCancelled:
    def is_requested(self):
        return False


class _StageOperations:
    def __init__(self):
        self.prepared = []
        self.marked = []
        self.failed = []

    async def prepare_agent_session(self, run_id, *, agent_role, target_comid=None):
        self.prepared.append((agent_role, target_comid))
        return type("Session", (), {"session_id": SUMMARY_SESSION_ID})()

    async def mark_agent_session_running(self, run_id, session_id):
        self.marked.append(session_id)

    async def fail_agent_session(self, run_id, session_id, error):
        self.failed.append(error)

    async def cancel_agent_session(self, run_id, session_id):
        return session_id


class _StageBuilder:
    async def build(self, **kwargs):
        return summary_input()


class _TooLargeStageBuilder:
    async def build(self, **kwargs):
        raise SummaryInputTooLargeError()


class _StageRunner:
    async def run(self, *args, **kwargs):
        return type("Outcome", (), {"status": RunSummaryRunStatus.CANCELLED})()


class _StageRunnerFactory:
    def build(self, *, run_id, session_id):
        assert session_id == SUMMARY_SESSION_ID
        return _StageRunner()


def test_stage_creates_one_targetless_session_only_after_all_active_outcomes_settle() -> None:
    graph = topology(("100", "300"), ("200", "300"))
    aggregation = TopologyResultAssembler(FakeResultReader()).aggregate(
        graph,
        (
            node_outcome("100"),
            node_outcome("200"),
            node_outcome("300"),
        ),
    )
    operations = _StageOperations()
    stage = RunSummaryStage(
        operations=operations,
        input_builder=_StageBuilder(),
        runner_factory=_StageRunnerFactory(),
    )
    arguments = {
        "run_id": FIXTURE_RUN_ID,
        "query": "Summarize.",
        "topology": graph,
        "topology_ref": TranscriptContentRef(
            run_id=FIXTURE_RUN_ID,
            item_id=INPUT_ITEM_ID,
        ),
        "aggregation": aggregation,
        "model": MODEL,
        "model_fit": ModelFitCapability(
            model=MODEL,
            context_window_tokens=10_000,
            max_output_tokens=1_000,
        ),
        "deadline": NOW,
        "cancellation": _NotCancelled(),
    }

    incomplete = replace(aggregation, outcomes=aggregation.outcomes[:-1])
    with pytest.raises(ValueError, match="all active outcomes"):
        asyncio.run(stage.run(**{**arguments, "aggregation": incomplete}))
    assert operations.prepared == []

    asyncio.run(stage.run(**arguments))
    assert [(role.value, target) for role, target in operations.prepared] == [
        ("run_summarizer", None)
    ]
    assert operations.marked == [SUMMARY_SESSION_ID]


def test_stage_saves_nonretryable_failure_when_complete_input_cannot_fit() -> None:
    graph = topology(("100", "300"))
    aggregation = TopologyResultAssembler(FakeResultReader()).aggregate(
        graph,
        (node_outcome("100"), node_outcome("300")),
    )
    operations = _StageOperations()
    stage = RunSummaryStage(
        operations=operations,
        input_builder=_TooLargeStageBuilder(),
        runner_factory=_StageRunnerFactory(),
    )
    result = asyncio.run(
        stage.run(
            run_id=FIXTURE_RUN_ID,
            query="Summarize.",
            topology=graph,
            topology_ref=TranscriptContentRef(
                run_id=FIXTURE_RUN_ID,
                item_id=INPUT_ITEM_ID,
            ),
            aggregation=aggregation,
            model=MODEL,
            model_fit=ModelFitCapability(
                model=MODEL,
                context_window_tokens=10_000,
                max_output_tokens=1_000,
            ),
            deadline=NOW,
            cancellation=_NotCancelled(),
        )
    )

    assert result.status is RunSummaryRunStatus.FAILED
    assert result.model_calls == 0
    assert operations.failed[0].code == "summary_input_too_large"
    assert operations.failed[0].retryable is False
