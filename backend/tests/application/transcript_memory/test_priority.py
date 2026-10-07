from app.application.reasoning import ReasoningGateDecision, seal_evidence_input
from app.domain import EvidenceCellRole
from tests.application.reasoning.test_sufficiency import _replace_window_statuses
from tests.application.reasoning.test_task_and_skeleton import (
    _async_test,
    _validated_fixture,
)


@_async_test
async def test_current_direct_evidence_is_authoritative_while_context_cannot_replace_it():
    validated, handoff, registry = await _validated_fixture(mode="full")
    current = seal_evidence_input(validated, handoff, registry)
    historical_only = seal_evidence_input(
        validated,
        _replace_window_statuses(
            handoff,
            target_status=handoff.cell_gates[0].target_status,
            discharge_status=handoff.cell_gates[0].discharge_status,
            move_target_to="contextual",
        ),
        registry,
    )
    current_target = next(
        cell
        for cell in current.cells
        if cell.role is EvidenceCellRole.TARGET
        and cell.window_id == current.gates[0].window_id
    )
    historical_target = next(
        cell
        for cell in historical_only.cells
        if cell.role is EvidenceCellRole.TARGET
        and cell.window_id == historical_only.gates[0].window_id
    )

    assert current_target.direct_fragment_refs
    assert current.gates[0].decision is ReasoningGateDecision.SUPPORTED
    assert set(current_target.direct_fragment_refs) <= set(
        current.gates[0].allowed_evidence_refs
    )
    assert historical_target.contextual_fragment_refs
    assert historical_only.gates[0].decision is ReasoningGateDecision.UNSUPPORTED
    assert set(historical_target.contextual_fragment_refs).isdisjoint(
        historical_only.gates[0].allowed_evidence_refs
    )
