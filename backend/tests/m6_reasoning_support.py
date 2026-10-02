import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from app.application.analysis_runs.models import AgentRole
from app.application.analysis_runs.workflow import CancellationToken, WorkflowContext
from app.application.evidence_tools import assemble_evidence_session_scope
from app.application.reasoning import (
    ValidatedWaterbodyAgentTask,
    WaterbodyAgentTask,
    validate_waterbody_agent_task,
)
from app.domain import ItemReference, ReferenceScope
from app.infrastructure.config import AppSettings, load_parameter_capability_registry
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from app.infrastructure.runtime import (
    OpenAIResponsesSizeEstimator,
    WaterbodyAdapterRegistration,
    compose_m5_runtime,
)
from app.infrastructure.runtime.m6_test import build_m6_test_runtime
from app.ports import PortUnavailableError
from tests.application.evidence_tools.conftest import make_context, make_graph
from tests.application.evidence_tools.fakes import complete_batch
from tests.application.evidence_tools.test_acquisition_workflow import _water_records
from tests.m4_support import (
    MODEL_CATALOG,
    PARSER_MODEL,
    WATERBODY_MODEL,
    create_request_bytes,
    create_test_app,
)


_DEFAULT_TRANSCRIPT_STORE = object()


class HoldingWorkflow:
    def __init__(self) -> None:
        self.calls: asyncio.Queue[WorkflowContext] = asyncio.Queue()

    async def run(self, context: WorkflowContext) -> None:
        self.calls.put_nowait(context)
        await context.cancellation.wait()
        context.cancellation.checkpoint()


class ScenarioWaterPort:
    def __init__(self, mode: str = "full") -> None:
        self.mode = mode
        self.scope = None
        self.calls = []

    def bind(self, scope) -> None:
        self.scope = scope

    async def collect(self, request, *, deadline=None):
        if self.scope is None:
            raise AssertionError("M6 smoke water port has no bound scope")
        self.calls.append((request, deadline))
        if self.mode == "failure":
            raise PortUnavailableError(
                "fixed M6 smoke provider unavailable",
                provider="m6_fixture",
                operation=request.operation_id,
            )
        include = self.mode in {"full", "incomplete"} or (
            self.mode == "partial"
            and request.parameter_keys[0] == self.scope.requested_parameters[0]
        ) or self.mode == "invalid"
        return complete_batch(
            request,
            _water_records(
                self.scope,
                request,
                include=include,
                invalid_unit=self.mode == "invalid",
            ),
            complete=self.mode != "incomplete",
        )


class EmptyWeatherPort:
    async def collect(self, request, *, deadline=None):
        del deadline
        return complete_batch(request)


class EmptyFacilityPort:
    async def collect(self, request, *, deadline=None):
        del deadline
        return complete_batch(request)


@dataclass(frozen=True, slots=True)
class PreparedWaterbody:
    run_id: UUID
    session_id: UUID
    task: ValidatedWaterbodyAgentTask


def create_reasoning_test_app(
    monkeypatch,
    *,
    model_port,
    water_mode: str = "full",
    settings: AppSettings | None = None,
    transcript_store=_DEFAULT_TRANSCRIPT_STORE,
    keepalive_interval: float = 0.01,
):
    import app.main as app_main

    water_port = ScenarioWaterPort(water_mode)

    @asynccontextmanager
    async def fake_m5_runtime(
        active_settings,
        *,
        operations,
        transcript_reader,
        capability_registry,
        clock,
    ) -> AsyncIterator[object]:
        yield compose_m5_runtime(
            active_settings,
            operations=operations,
            transcript_reader=transcript_reader,
            capability_registry=capability_registry,
            water_port=water_port,
            weather_port=EmptyWeatherPort(),
            facility_port=EmptyFacilityPort(),
            clock=clock,
            operation_id_factory=lambda: str(uuid4()),
        )

    @asynccontextmanager
    async def fake_m6_runtime(active_settings, **kwargs):
        async with build_m6_test_runtime(
            active_settings,
            registrations=(
                WaterbodyAdapterRegistration(
                    provider=WATERBODY_MODEL.provider,
                    port=model_port,
                    size_estimator=OpenAIResponsesSizeEstimator(),
                ),
            ),
            private_values=("m6-fixture-secret",),
            **kwargs,
        ) as runtime:
            yield runtime

    monkeypatch.setattr(app_main, "build_m5_runtime", fake_m5_runtime)
    workflow = HoldingWorkflow()
    app_options = {
        "workflow": workflow,
        "model_catalog": MODEL_CATALOG,
        "shutdown_grace_period": 0,
        "keepalive_interval": keepalive_interval,
        "m6_runtime_builder": fake_m6_runtime,
    }
    if transcript_store is _DEFAULT_TRANSCRIPT_STORE:
        app_options["transcript_store"] = InMemoryTranscriptStore()
    elif transcript_store is not None:
        app_options["transcript_store"] = transcript_store
    application = create_test_app(
        settings or AppSettings(_env_file=None),
        **app_options,
    )
    return application, workflow, water_port


async def prepare_waterbody(
    application,
    workflow: HoldingWorkflow,
    water_port: ScenarioWaterPort,
    *,
    query: str = "M6 fixed single-waterbody smoke",
    cancellation_event: asyncio.Event | None = None,
) -> PreparedWaterbody:
    coordinator = application.state.run_coordinator
    created = await coordinator.create_run(create_request_bytes(query))
    await workflow.calls.get()
    parser = await coordinator.prepare_agent_session(
        created.run_id,
        agent_role=AgentRole.QUERY_PARSER,
    )
    await coordinator.mark_agent_session_running(created.run_id, parser.session_id)
    context = make_context().model_copy(
        update={
            "parser_model": PARSER_MODEL,
            "waterbody_model": WATERBODY_MODEL,
        }
    )
    context_item = await coordinator.reserve_transcript_item(
        created.run_id,
        session_id=parser.session_id,
    )
    await coordinator.finalize_transcript_item(
        created.run_id,
        context_item.item_id,
        "query_understood",
        1,
        context,
    )
    graph = make_graph()
    topology_item = await coordinator.reserve_transcript_item(created.run_id)
    await coordinator.finalize_transcript_item(
        created.run_id,
        topology_item.item_id,
        "topology_ready",
        1,
        graph,
    )
    await coordinator.complete_agent_session(created.run_id, parser.session_id)
    session = await coordinator.prepare_agent_session(
        created.run_id,
        agent_role=AgentRole.WATERBODY,
        target_comid=graph.target_comid,
    )
    cancel_event = cancellation_event or asyncio.Event()
    scope = assemble_evidence_session_scope(
        run_id=str(created.run_id),
        session_id=str(session.session_id),
        session_fact=ReferenceScope(
            item_id=str(session.session_id),
            run_id=str(created.run_id),
            session_id=str(session.session_id),
        ),
        analysis_context_ref=ReferenceScope(
            item_id=str(context_item.item_id),
            run_id=str(created.run_id),
        ),
        topology_ref=ReferenceScope(
            item_id=str(topology_item.item_id),
            run_id=str(created.run_id),
        ),
        context=context,
        topology=graph,
        agent_comid=graph.target_comid,
        registry=load_parameter_capability_registry(),
        cancel_token=CancellationToken(cancel_event),
    )
    water_port.bind(scope)
    task = WaterbodyAgentTask(
        run_id=created.run_id,
        session_id=session.session_id,
        target_comid=graph.target_comid,
        analysis_context_ref=ItemReference(item_id=scope.analysis_context_ref.item_id),
        topology_ref=ItemReference(item_id=scope.topology_ref.item_id),
        evidence_scope=scope,
        waterbody_model=context.waterbody_model,
        memory_mode=context.memory_mode,
        cancellation_token=scope.cancel_token,
    )
    validated = await validate_waterbody_agent_task(
        task,
        run=await coordinator.get_snapshot(created.run_id),
        context=context,
        topology=graph,
        model_catalog=MODEL_CATALOG.snapshot(),
        session_reader=application.state.m5_runtime.transcript,
    )
    await coordinator.mark_agent_session_running(created.run_id, session.session_id)
    return PreparedWaterbody(
        run_id=created.run_id,
        session_id=session.session_id,
        task=validated,
    )


async def run_prepared(application, prepared: PreparedWaterbody):
    return await application.state.m6_runtime.runner.run(
        prepared.task,
        deadline=application.state.m6_runtime.runner.session_deadline()
        - timedelta(seconds=1),
    )


async def cancel_run(application, run_id: UUID) -> None:
    await application.state.run_coordinator.cancel_run(run_id)
    runtime = application.state.run_coordinator._runs[run_id]
    await runtime.workflow_quiesced.wait()
