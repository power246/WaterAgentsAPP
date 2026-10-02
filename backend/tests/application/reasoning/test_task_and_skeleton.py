import asyncio
import copy
from dataclasses import replace
from datetime import date, datetime, timezone
from functools import wraps
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.application.analysis_runs.models import (
    AgentRole,
    AgentSessionStatus,
    AnalysisConfigurationSnapshot,
    AnalysisRunSnapshot,
    RunStatus,
    SessionSummary,
)
from app.application.evidence_tools import EvidenceAcquisitionCellGate
from app.application.reasoning import (
    AllCellsBlockedError,
    ReasoningCellClaim,
    ReasoningGateDecision,
    ResultSkeleton,
    ResultSkeletonValidationError,
    WaterbodyAgentTask,
    WaterbodyReasoningStage,
    WaterbodyTaskValidationError,
    build_result_skeleton,
    seal_evidence_input,
    validate_agent_cell_claims,
    validate_result_skeleton,
    validate_waterbody_agent_task,
)
from app.domain import (
    EvidenceBundle,
    EvidenceCellRole,
    EvidenceCellStatus,
    IndeterminateReason,
    ItemReference,
    MemoryMode,
    ModelCapability,
    ModelCatalogSnapshot,
    ModelRole,
    ModelSelection,
    OutcomeStatus,
    ReferenceScope,
    TimeResolutionPreference,
    TimeResolutionPreferenceSource,
)
from app.infrastructure.config import load_parameter_capability_registry
from tests.application.evidence_tools.conftest import (
    MODEL,
    make_context,
    make_graph,
    make_scope,
)
from tests.application.evidence_tools.fakes import MemoryEvidenceStore
from tests.application.evidence_tools.test_acquisition_workflow import (
    _runtime,
    _water_port,
)


RUN_ID = UUID("00000000-0000-0000-0000-000000000001")
SESSION_ID = UUID("00000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


def _run(context, *, model=None, memory_mode=None):
    return AnalysisRunSnapshot(
        run_id=RUN_ID,
        status=RunStatus.RUNNING,
        created_at=NOW,
        updated_at=NOW,
        configuration=AnalysisConfigurationSnapshot(
            analysis_timezone=context.analysis_timezone,
            time_resolution_preference=TimeResolutionPreference.AUTO,
            time_resolution_preference_source=TimeResolutionPreferenceSource.FRONTEND,
            parser_model=context.parser_model,
            waterbody_model=model or context.waterbody_model,
            memory_mode=memory_mode or context.memory_mode,
        ),
    )


def _session(
    *,
    role=AgentRole.WATERBODY,
    target_comid="123",
    status=AgentSessionStatus.PENDING,
):
    return SessionSummary(
        session_id=SESSION_ID,
        agent_role=role,
        target_comid=target_comid,
        status=status,
        created_at=NOW,
        updated_at=NOW,
    )


def _catalog(model=MODEL, *, role=ModelRole.WATERBODY):
    return ModelCatalogSnapshot(
        capabilities=(
            ModelCapability(
                provider=model.provider,
                model=model.model,
                roles=(role,),
            ),
        )
    )


def _task(scope, *, model=MODEL, memory_mode=MemoryMode.STATELESS):
    return WaterbodyAgentTask(
        run_id=RUN_ID,
        session_id=SESSION_ID,
        target_comid="123",
        analysis_context_ref=ItemReference(item_id="context-item"),
        topology_ref=ItemReference(item_id="topology-item"),
        evidence_scope=scope,
        waterbody_model=model,
        memory_mode=memory_mode,
        cancellation_token=scope.cancel_token,
    )


def _scope_copy(scope, **changes):
    copied = copy.copy(scope)
    for name, value in changes.items():
        object.__setattr__(copied, name, value)
    return copied


async def _validated_fixture(
    *,
    mode="full",
    with_station=True,
    context=None,
):
    context = context or make_context()
    topology = make_graph(with_station=with_station)
    scope = make_scope(
        run_id=str(RUN_ID),
        session_id=str(SESSION_ID),
        context=context,
        with_station=with_station,
    )
    store = MemoryEvidenceStore()
    store.seed_session(_session())
    validated = await validate_waterbody_agent_task(
        _task(scope, memory_mode=context.memory_mode),
        run=_run(context),
        context=context,
        topology=topology,
        model_catalog=_catalog(),
        session_reader=store,
    )
    service = _runtime(
        store,
        water_port=_water_port(scope, mode=mode) if with_station else None,
    )
    handoff = await service.acquire(scope)
    return validated, handoff, load_parameter_capability_registry()


@_async_test
async def test_task_validator_accepts_only_the_saved_waterbody_boundary():
    context = make_context()
    topology = make_graph()
    scope = make_scope(run_id=str(RUN_ID), session_id=str(SESSION_ID), context=context)
    store = MemoryEvidenceStore()
    store.seed_session(_session())

    validated = await validate_waterbody_agent_task(
        _task(scope),
        run=_run(context),
        context=context,
        topology=topology,
        model_catalog=_catalog(),
        session_reader=store,
    )

    assert validated.task.evidence_scope is scope
    assert validated.session.status is AgentSessionStatus.PENDING
    assert tuple(stage.value for stage in WaterbodyReasoningStage) == (
        "preflight",
        "evidence_planning",
        "evidence_sealed",
        "reasoning_batches",
        "overall_synthesis",
        "assembling_result",
        "terminal",
    )
    assert not set(WaterbodyReasoningStage) & set(AgentSessionStatus)


@_async_test
async def test_task_validator_rejects_identity_scope_model_and_session_drift():
    context = make_context()
    topology = make_graph()

    async def rejected(
        code,
        *,
        task_mutator=lambda task: task,
        scope_mutator=lambda scope: scope,
        run=None,
        supplied_context=None,
        supplied_topology=None,
        session=None,
        catalog=None,
        seed_session=True,
    ):
        scope = scope_mutator(
            make_scope(
                run_id=str(RUN_ID),
                session_id=str(SESSION_ID),
                context=context,
            )
        )
        store = MemoryEvidenceStore()
        if seed_session:
            store.seed_session(session or _session())
        with pytest.raises(WaterbodyTaskValidationError) as raised:
            await validate_waterbody_agent_task(
                task_mutator(_task(scope)),
                run=run or _run(context),
                context=supplied_context or context,
                topology=supplied_topology or topology,
                model_catalog=catalog or _catalog(),
                session_reader=store,
            )
        assert raised.value.code == code

    await rejected("task_session_not_found", seed_session=False)
    await rejected(
        "task_session_scope_mismatch",
        scope_mutator=lambda scope: _scope_copy(
            scope,
            session_ref=ReferenceScope(
                item_id="session-fact",
                run_id="another-run",
                session_id=str(SESSION_ID),
            ),
        ),
    )
    await rejected(
        "task_session_role_mismatch",
        session=_session(role=AgentRole.QUERY_PARSER, target_comid=None),
    )
    await rejected("task_session_comid_mismatch", session=_session(target_comid="999"))
    await rejected(
        "task_context_ref_mismatch",
        task_mutator=lambda task: replace(
            task,
            analysis_context_ref=ItemReference(item_id="wrong-context"),
        ),
    )
    await rejected(
        "task_topology_ref_mismatch",
        task_mutator=lambda task: replace(
            task,
            topology_ref=ItemReference(item_id="wrong-topology"),
        ),
    )
    wrong_model = ModelSelection(provider="openai", model="other-model")
    await rejected(
        "task_model_selection_mismatch",
        task_mutator=lambda task: replace(task, waterbody_model=wrong_model),
    )
    await rejected(
        "task_model_role_unsupported",
        catalog=_catalog(role=ModelRole.QUERY_PARSER),
    )
    await rejected(
        "task_memory_mode_mismatch",
        task_mutator=lambda task: replace(
            task,
            memory_mode=MemoryMode.MEMORY_AUGMENTED,
        ),
    )
    await rejected(
        "task_scope_requested_parameters_mismatch",
        scope_mutator=lambda scope: _scope_copy(scope, requested_parameters=()),
    )
    await rejected(
        "task_scope_topology_mismatch",
        supplied_topology=make_graph(with_station=False),
    )
    await rejected(
        "task_session_not_startable",
        session=_session(status=AgentSessionStatus.CANCELLED),
    )


@_async_test
async def test_task_cancel_accepted_fails_before_any_session_or_operation_read():
    context = make_context()
    cancel_event = asyncio.Event()
    cancel_event.set()
    scope = make_scope(
        run_id=str(RUN_ID),
        session_id=str(SESSION_ID),
        context=context,
        cancel_event=cancel_event,
    )

    class NoReadAllowed:
        calls = 0

        async def read_agent_session(self, scope, session_id):
            self.calls += 1
            raise AssertionError("cancelled task must not read or start operations")

    reader = NoReadAllowed()
    with pytest.raises(WaterbodyTaskValidationError) as raised:
        await validate_waterbody_agent_task(
            _task(scope),
            run=_run(context),
            context=context,
            topology=make_graph(),
            model_catalog=_catalog(),
            session_reader=reader,
        )
    assert raised.value.code == "task_cancelled_before_start"
    assert reader.calls == 0


@_async_test
async def test_supported_handoff_builds_stable_skeleton_and_narrow_agent_projection():
    validated, handoff, registry = await _validated_fixture(mode="full")
    first_sealed = seal_evidence_input(validated, handoff, registry)
    second_sealed = seal_evidence_input(validated, handoff, registry)
    first = build_result_skeleton(
        validated,
        first_sealed,
        registry,
        result_id="result-fixed",
        generated_at=NOW,
    )
    second = build_result_skeleton(
        validated,
        second_sealed,
        registry,
        result_id="result-fixed",
        generated_at=NOW,
    )

    assert first_sealed.canonical_bytes() == second_sealed.canonical_bytes()
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.skeleton_digest == second.skeleton_digest
    assert first.planned_cell_count == len(first.windows)
    assert first.supported_cell_count == len(first.windows)
    assert first.fixed_cells == ()
    assert all(scope.allowed_evidence_refs for scope in first.supported_scopes)
    assert all(scope.allowed_unit_ids == ("mg/l",) for scope in first.supported_scopes)
    assert all(scope.allowed_result_sources for scope in first.supported_scopes)
    assert all(scope.allowed_aggregation_methods for scope in first.supported_scopes)

    projection = first.agent_projection()
    payload = projection.model_dump(mode="json")
    for server_field in (
        "run_id",
        "session_id",
        "result_id",
        "generated_at",
        "planned_cell_count",
        "supported_cell_count",
        "fixed_cells",
    ):
        assert server_field not in payload
    assert len(projection.cells) == first.supported_cell_count


@_async_test
async def test_natural_unsupported_builds_complete_fixed_indeterminate_skeleton():
    validated, handoff, registry = await _validated_fixture(mode="empty")
    sealed = seal_evidence_input(validated, handoff, registry)
    skeleton = build_result_skeleton(
        validated,
        sealed,
        registry,
        result_id="result-empty",
        generated_at=NOW,
    )

    assert {gate.decision for gate in sealed.gates} == {
        ReasoningGateDecision.UNSUPPORTED
    }
    assert skeleton.supported_scopes == ()
    assert skeleton.fixed_cell_count == skeleton.planned_cell_count
    assert skeleton.blocked_cell_count == 0
    assert skeleton.agent_projection().cells == ()
    for fixed in skeleton.fixed_cells:
        point = fixed.result
        assert point.outcome_status is OutcomeStatus.INDETERMINATE
        assert point.representative_value is None
        assert point.unit_id is None
        assert point.source is None
        assert point.confidence == 0
        assert point.assessment.reason_codes == (
            IndeterminateReason.INSUFFICIENT_EVIDENCE,
        )
        assert point.evidence_refs
        assert fixed.blocker_refs == ()


@_async_test
async def test_all_technical_blocked_forms_failure_input_not_business_result():
    validated, handoff, registry = await _validated_fixture(mode="incomplete")
    sealed = seal_evidence_input(validated, handoff, registry)

    assert {gate.decision for gate in sealed.gates} == {ReasoningGateDecision.BLOCKED}
    assert all(gate.blocker_refs for gate in sealed.gates)
    with pytest.raises(AllCellsBlockedError):
        build_result_skeleton(
            validated,
            sealed,
            registry,
            result_id="result-blocked",
            generated_at=NOW,
        )


@_async_test
async def test_partial_technical_block_preserves_explicit_fixed_blocker():
    validated, handoff, registry = await _validated_fixture(mode="full")
    blocked_window = handoff.bundle.window_refs[-1]
    cells = []
    for cell in handoff.cells:
        if cell.window_id != blocked_window:
            cells.append(cell)
            continue
        cells.append(
            cell.model_copy(
                update={
                    "direct_fragment_refs": (),
                    "allowed_indirect_fragment_refs": (),
                    "contextual_fragment_refs": tuple(
                        sorted(
                            {
                                *cell.contextual_fragment_refs,
                                *cell.direct_fragment_refs,
                            }
                        )
                    ),
                    "status": EvidenceCellStatus.BLOCKED,
                    "reasons": ("provider_timeout",),
                }
            )
        )
    bundle = EvidenceBundle.model_validate(
        handoff.bundle.model_copy(update={"cells": tuple(cells)}).model_dump()
    )
    gates = []
    for gate in handoff.cell_gates:
        if gate.window_id == blocked_window:
            gates.append(
                EvidenceAcquisitionCellGate(
                    parameter=gate.parameter,
                    window_id=gate.window_id,
                    target_status=EvidenceCellStatus.BLOCKED,
                    discharge_status=EvidenceCellStatus.BLOCKED,
                    status=EvidenceCellStatus.BLOCKED,
                    reasons=(
                        "target:provider_timeout",
                        "required_discharge:provider_timeout",
                    ),
                )
            )
        else:
            gates.append(gate)
    handoff = replace(
        handoff,
        bundle=bundle,
        cells=tuple(cells),
        cell_gates=tuple(gates),
        block_facts=("provider_timeout",),
    )
    sealed = seal_evidence_input(validated, handoff, registry)
    skeleton = build_result_skeleton(
        validated,
        sealed,
        registry,
        result_id="result-partial",
        generated_at=NOW,
    )

    assert skeleton.supported_cell_count == skeleton.planned_cell_count - 1
    assert skeleton.blocked_cell_count == 1
    fixed = skeleton.fixed_cells[0]
    assert fixed.decision is ReasoningGateDecision.BLOCKED
    assert fixed.result.assessment.reason_codes == (
        IndeterminateReason.SOURCE_UNAVAILABLE,
    )
    assert fixed.blocker_refs
    assert "provider_timeout" in " ".join(fixed.reasons)


@_async_test
async def test_skeleton_rejects_identity_counts_refs_and_fixed_cell_claims():
    validated, handoff, registry = await _validated_fixture(mode="full")
    sealed = seal_evidence_input(validated, handoff, registry)
    skeleton = build_result_skeleton(
        validated,
        sealed,
        registry,
        result_id="result-guarded",
        generated_at=NOW,
    )
    scope = skeleton.supported_scopes[0]
    valid_claim = ReasoningCellClaim(
        cell_id=scope.cell_id,
        parameter=scope.parameter,
        window_id=scope.window_id,
        evidence_refs=(scope.allowed_evidence_refs[0],),
    )
    assert validate_agent_cell_claims(skeleton, (valid_claim,)) == (valid_claim,)

    fixed_validated, fixed_handoff, fixed_registry = await _validated_fixture(mode="empty")
    fixed_skeleton = build_result_skeleton(
        fixed_validated,
        seal_evidence_input(fixed_validated, fixed_handoff, fixed_registry),
        fixed_registry,
        result_id="result-fixed-guarded",
        generated_at=NOW,
    )
    fixed = fixed_skeleton.fixed_cells[0]
    with pytest.raises(ResultSkeletonValidationError) as raised:
        validate_agent_cell_claims(
            fixed_skeleton,
            (
                ReasoningCellClaim(
                    cell_id=fixed.result.cell_id,
                    parameter=fixed_skeleton.requested_parameters[0],
                    window_id=fixed_skeleton.windows[0].window_id,
                    evidence_refs=fixed.result.evidence_refs,
                ),
            ),
        )
    assert raised.value.code == "reasoning_cell_not_supported"

    malicious = valid_claim.model_dump(mode="json") | {
        "run_id": str(RUN_ID),
        "planned_cell_count": 999,
    }
    with pytest.raises(ValidationError):
        validate_agent_cell_claims(skeleton, (malicious,))
    with pytest.raises(ResultSkeletonValidationError) as raised:
        validate_agent_cell_claims(
            skeleton,
            (
                valid_claim.model_copy(
                    update={"evidence_refs": ("evidence:outside-allowlist",)}
                ),
            ),
        )
    assert raised.value.code == "reasoning_evidence_out_of_scope"
    with pytest.raises(ResultSkeletonValidationError) as raised:
        validate_agent_cell_claims(
            skeleton,
            (
                valid_claim.model_copy(
                    update={"cell_id": "cell:usgs:99999:window-999"}
                ),
            ),
        )
    assert raised.value.code == "reasoning_cell_not_supported"
    with pytest.raises(ResultSkeletonValidationError) as raised:
        validate_agent_cell_claims(
            skeleton,
            (
                valid_claim.model_copy(
                    update={"window_id": skeleton.windows[-1].window_id}
                ),
            ),
        )
    assert raised.value.code == "reasoning_cell_identity_mismatch"

    for field, value in (
        ("run_id", UUID("00000000-0000-0000-0000-000000000099")),
        ("planned_cell_count", 999),
        ("evidence_allowlist", ("evidence:tampered",)),
        ("skeleton_digest", "sha256:tampered"),
    ):
        tampered = skeleton.model_copy(update={field: value})
        with pytest.raises(ValidationError):
            validate_result_skeleton(tampered)


@_async_test
async def test_mixed_dst_windows_keep_stable_segment_and_parameter_order():
    context = make_context(
        start_date=date(2026, 3, 7),
        end_date=date(2026, 3, 9),
        analysis_timezone="America/New_York",
        decision_instant=datetime(2026, 3, 8, 12, tzinfo=timezone.utc),
    )
    validated, handoff, registry = await _validated_fixture(
        mode="full",
        context=context,
    )
    sealed = seal_evidence_input(validated, handoff, registry)
    skeleton = build_result_skeleton(
        validated,
        sealed,
        registry,
        result_id="result-mixed-dst",
        generated_at=NOW,
    )

    assert skeleton.mode.value == "mixed"
    assert tuple(window.start_time for window in skeleton.windows) == tuple(
        sorted(window.start_time for window in skeleton.windows)
    )
    assert len({window.window_id for window in skeleton.windows}) == len(
        skeleton.windows
    )
    assert tuple(window.mode.value for window in skeleton.windows) == tuple(
        sorted(
            (window.mode.value for window in skeleton.windows),
            key={"gap_filling": 0, "prediction": 1}.__getitem__,
        )
    )
    assert skeleton.cell_order == tuple(gate.cell_id for gate in sealed.gates)
    assert all(
        scope.allowed_result_sources[0].value == "predicted"
        for scope in skeleton.supported_scopes
        if next(
            window for window in skeleton.windows if window.window_id == scope.window_id
        ).mode.value
        == "prediction"
    )
    assert skeleton.canonical_bytes() == validate_result_skeleton(
        skeleton
    ).canonical_bytes()
