import ast
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.application.analysis_runs.topology_driven_workflow import (
    TopologyWaterbodyExecutionStage,
)
from app.application.analysis_runs.models import (
    AgentRole,
    AgentSessionStatus,
    RunStatus,
    RunSummarySessionResultSummary,
    SessionResultReference,
    SessionSummary,
)
from app.application.analysis_runs.topology_result_assembler import (
    TopologyResultAssembler,
)
from app.application.analysis_runs.workflow import CancellationToken, WorkflowContext
from app.application.run_summary.models import RunSummaryResult
from app.application.run_summary.runner import (
    RunSummaryRunOutcome,
    RunSummaryRunStatus,
)
from app.application.run_summary.stage import RunSummaryStageOutcome
from app.application.transcripts.models import TranscriptContentRef
from app.application.reasoning.batch_planner import ModelFitCapability
from app.infrastructure.config import AppSettings, SettingsConfigurationError
from app.infrastructure.runtime.m7 import (
    CompleteRunSummaryBoundary,
    ProductionRunSummaryRunnerFactory,
    compose_m7_execution_stage,
)
from tests.application.analysis_runs._topology_scheduler_support import (
    RUN_ID,
    outcome,
    topology,
)
from tests.application.evidence_tools.test_upstream_result_store import FakeResultReader
from tests.m4_support import MODEL_CATALOG, WATERBODY_MODEL, analysis_context


BACKEND_ROOT = Path(__file__).resolve().parents[2]


class _Router:
    def __init__(self, providers=("test-provider",)) -> None:
        self.providers = providers

    def estimate(self, *args, **kwargs):  # pragma: no cover - composition only
        raise AssertionError("size estimation is not expected during composition")


def _composition(*, providers=("test-provider",)):
    router = _Router(providers)
    operations = SimpleNamespace()
    m6_runtime = SimpleNamespace(
        runner=SimpleNamespace(),
        model_port_router=router,
        private_values=("private-provider-key",),
    )
    stage = compose_m7_execution_stage(
        AppSettings(_env_file=None),
        operations=operations,
        m5_runtime=SimpleNamespace(
            acquisition=SimpleNamespace(),
            transcript=SimpleNamespace(),
        ),
        m6_runtime=m6_runtime,
        upstream_results=SimpleNamespace(),
        result_reader=SimpleNamespace(),
        capability_registry=SimpleNamespace(),
        model_catalog=MODEL_CATALOG.snapshot(),
        clock=SimpleNamespace(now=lambda: None),
        result_id_factory=lambda: "result-id",
        error_id_factory=lambda: None,
        operation_id_factory=lambda: None,
    )
    return stage, router, m6_runtime


def test_production_m7_composition_owns_one_scheduler_and_summary_boundary() -> None:
    stage, router, m6_runtime = _composition()

    assert isinstance(stage, TopologyWaterbodyExecutionStage)
    assert isinstance(stage._summary_boundary, CompleteRunSummaryBoundary)
    factory = stage._summary_boundary._summary_stage._runner_factory
    assert isinstance(factory, ProductionRunSummaryRunnerFactory)
    assert factory._m6_runtime is m6_runtime
    assert stage._summary_boundary._m6_runtime.model_port_router is router
    assert stage._limiter.max_concurrency == 4
    assert stage._summary_boundary._timeout_seconds == 120


def test_m7_runtime_fails_closed_without_a_waterbody_provider() -> None:
    with pytest.raises(SettingsConfigurationError, match="waterbody model provider"):
        _composition(providers=())


def test_summary_boundary_uses_run_model_and_commits_one_topology_result() -> None:
    now = datetime(2026, 10, 3, 12, tzinfo=UTC)
    summary_session_id = UUID("50000000-0000-0000-0000-000000000001")
    summary_ref = SessionResultReference(
        run_id=RUN_ID,
        item_id=UUID("50000000-0000-0000-0000-000000000002"),
    )
    summary_result = RunSummaryResult(
        run_id=RUN_ID,
        session_id=summary_session_id,
        input_digest="sha256:runtime-summary",
        model=WATERBODY_MODEL,
        language="en",
        plain_language_summary="The target result is available.",
        generated_at=now,
    )

    class Router(_Router):
        def __init__(self) -> None:
            super().__init__()
            self.models = []

        def model_fit(self, model):
            self.models.append(model)
            return ModelFitCapability(
                model=model,
                context_window_tokens=10_000,
                max_output_tokens=1_000,
            )

    class Stage:
        def __init__(self) -> None:
            self.calls = []

        async def run(self, **kwargs):
            self.calls.append(kwargs)
            return RunSummaryStageOutcome(
                session_id=summary_session_id,
                outcome=RunSummaryRunOutcome(
                    status=RunSummaryRunStatus.COMPLETED,
                    result=summary_result,
                    error=None,
                    model_calls=1,
                    repaired=False,
                ),
            )

    class Operations:
        def __init__(self) -> None:
            self.results = []

        async def get_session(self, run_id, session_id):
            return SessionSummary(
                session_id=session_id,
                agent_role=AgentRole.RUN_SUMMARIZER,
                status=AgentSessionStatus.COMPLETED,
                created_at=now,
                updated_at=now,
                result_ref=summary_ref,
                result_summary=RunSummarySessionResultSummary(
                    language="en",
                    highlight_count=0,
                ),
            )

        async def complete_run(self, run_id, result):
            self.results.append(result)
            return SimpleNamespace(status=RunStatus.COMPLETED)

    graph = topology(("100", "300"))
    base_context = analysis_context()
    saved_context = base_context.model_copy(update={
        "target": base_context.target.model_copy(update={"comid": "300"}),
    })
    assembler = TopologyResultAssembler(FakeResultReader())
    aggregation = assembler.aggregate(
        graph,
        (outcome("100"), outcome("300")),
    )
    router = Router()
    stage = Stage()
    operations = Operations()
    boundary = CompleteRunSummaryBoundary(
        operations=operations,
        summary_stage=stage,
        result_assembler=assembler,
        m6_runtime=SimpleNamespace(model_port_router=router),
        clock=SimpleNamespace(now=lambda: now),
        timeout_seconds=120,
    )
    context = WorkflowContext(
        run=SimpleNamespace(run_id=RUN_ID),
        query=saved_context.query,
        cancellation=CancellationToken(asyncio.Event()),
        request=SimpleNamespace(),
        operations=operations,
    )

    asyncio.run(boundary.wait(
        context,
        saved_context,
        graph,
        TranscriptContentRef(
            run_id=RUN_ID,
            item_id=UUID("50000000-0000-0000-0000-000000000003"),
        ),
        TranscriptContentRef(
            run_id=RUN_ID,
            item_id=UUID("50000000-0000-0000-0000-000000000004"),
        ),
        aggregation,
    ))

    assert router.models == [WATERBODY_MODEL]
    assert stage.calls[0]["model"] == WATERBODY_MODEL
    assert stage.calls[0]["deadline"] == now + timedelta(seconds=120)
    assert len(operations.results) == 1
    assert operations.results[0].summary.result_ref == summary_ref


@pytest.mark.parametrize("value", (0, -1, 301, True, float("inf")))
def test_run_summary_timeout_rejects_invalid_values(value) -> None:
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None, run_summary_timeout_seconds=value)


def test_formal_entrypoint_and_docs_keep_the_single_worker_boundary() -> None:
    entrypoint = (BACKEND_ROOT / "app/__main__.py").read_text(encoding="utf-8")
    tree = ast.parse(entrypoint)
    uvicorn_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
    ]
    assert len(uvicorn_calls) == 1
    workers = next(
        keyword.value
        for keyword in uvicorn_calls[0].keywords
        if keyword.arg == "workers"
    )
    assert isinstance(workers, ast.Constant) and workers.value == 1

    readme = (BACKEND_ROOT / "README.md").read_text(encoding="utf-8")
    assert "Do not run multiple application workers or replicas" in readme
    assert "not shared across workers" in readme
