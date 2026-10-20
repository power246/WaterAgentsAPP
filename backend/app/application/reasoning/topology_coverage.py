"""Bounded prompt facts from the saved upstream branch search."""

from collections import defaultdict

from app.application.evidence_tools.scope import EvidenceSessionScope
from app.application.reasoning.working_context import (
    WorkingContextFact,
    WorkingContextFactKind,
)
from app.domain import BranchOutcomeStatus, ParameterKey


def topology_coverage_facts(
    scope: EvidenceSessionScope,
    *,
    parameter: ParameterKey | None = None,
    window_id: str | None = None,
) -> tuple[WorkingContextFact, ...]:
    """Describe only search gaps upstream of this agent and selected cells."""

    windows = tuple(enumerate(scope.time_windows))
    if window_id is not None and not any(
        window.window_id == window_id for _, window in windows
    ):
        raise ValueError("topology coverage window must belong to the scope")
    grouped: dict[
        tuple[ParameterKey, int],
        dict[tuple[BranchOutcomeStatus, str], set[tuple[str, str]]],
    ] = defaultdict(lambda: defaultdict(set))
    for outcome in scope.topology.branch_outcomes:
        if (
            outcome.outcome
            not in {
                BranchOutcomeStatus.NO_QUALIFYING_SOURCE,
                BranchOutcomeStatus.SEARCH_LIMIT_REACHED,
            }
            or scope.agent_comid not in outcome.path_to_target
        ):
            continue
        unit = outcome.coverage_unit
        if parameter is not None and unit.parameter != parameter:
            continue
        for index, window in windows:
            if (
                window.segment_index != unit.segment_index
                or unit.window_index is not None
                and unit.window_index != index
                or window_id is not None
                and window.window_id != window_id
            ):
                continue
            grouped[(unit.parameter, unit.segment_index)][
                (outcome.outcome, outcome.reason.value)
            ].add((outcome.branch_root_comid, window.window_id))

    facts = []
    for (key, segment_index), gaps in sorted(
        grouped.items(),
        key=lambda item: (item[0][1], item[0][0].code_system, item[0][0].code),
    ):
        affected_windows = sorted(
            {window for pairs in gaps.values() for _, window in pairs}
        )
        details = []
        for (status, reason), pairs in sorted(
            gaps.items(), key=lambda item: (item[0][0].value, item[0][1])
        ):
            branches = _branch_windows(pairs)
            if status is BranchOutcomeStatus.NO_QUALIFYING_SOURCE:
                details.append(
                    "reached a natural headwater without a qualifying source: "
                    f"{branches}"
                )
            else:
                details.append(
                    f"search stopped at {reason}; source availability beyond "
                    f"the limit is unknown: {branches}"
                )
        facts.append(
            WorkingContextFact(
                fact_id=(
                    f"topology_gap:{key.code_system}:{key.code}:"
                    f"segment-{segment_index:03d}:windows:"
                    f"{','.join(affected_windows)}"
                ),
                kind=WorkingContextFactKind.COVERAGE,
                summary=(
                    f"Saved topology {scope.topology_ref.item_id}, parameter "
                    f"{key.code_system}:{key.code}: {'; '.join(details)}. "
                    "A missing qualifying source is not a zero reading. "
                    "Treat this as an uncertainty in source coverage, not a fixed "
                    "confidence penalty or a reason to suppress a useful estimate."
                ),
            )
        )
    return tuple(facts)


def topology_fact_applies_to_cell(
    fact: WorkingContextFact,
    *,
    parameter: ParameterKey,
    segment_index: int,
    window_id: str,
) -> bool:
    prefix = (
        f"topology_gap:{parameter.code_system}:{parameter.code}:"
        f"segment-{segment_index:03d}:windows:"
    )
    return fact.fact_id.startswith(prefix) and window_id in fact.fact_id[
        len(prefix) :
    ].split(",")


def _branch_windows(pairs: set[tuple[str, str]]) -> str:
    windows_by_root: dict[str, set[str]] = defaultdict(set)
    for root, window in pairs:
        windows_by_root[root].add(window)
    roots_by_windows: dict[tuple[str, ...], set[str]] = defaultdict(set)
    for root, windows in windows_by_root.items():
        roots_by_windows[tuple(sorted(windows))].add(root)
    return "; ".join(
        f"branch roots {_short_list(roots)} in {_short_list(set(windows))}"
        for windows, roots in sorted(roots_by_windows.items())
    )


def _short_list(values: set[str]) -> str:
    ordered = sorted(
        values, key=lambda value: (int(value), value) if value.isdigit() else (0, value)
    )
    if len(ordered) <= 8:
        return ", ".join(ordered)
    return ", ".join(ordered[:8]) + f", and {len(ordered) - 8} more"
