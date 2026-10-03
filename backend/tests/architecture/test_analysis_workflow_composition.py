import ast
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = BACKEND_ROOT / "app"


def _source_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=path)


def _named_callers(name: str) -> tuple[Path, ...]:
    callers = []
    for source_path in sorted(APP_ROOT.rglob("*.py")):
        for node in ast.walk(_source_tree(source_path)):
            if not isinstance(node, ast.Call):
                continue
            called_name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else None
            )
            if called_name == name:
                callers.append(source_path.relative_to(APP_ROOT))
    return tuple(callers)


def _calls_name(path: Path, name: str) -> bool:
    return any(
        isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == name)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
        )
        for node in ast.walk(_source_tree(path))
    )


def test_production_topology_execution_has_one_owner_and_binding() -> None:
    assert _named_callers("QueryTopologyWorkflow") == (
        Path("infrastructure/runtime/m4.py"),
    )
    assert _named_callers("bind_execution_stage") == (Path("main.py"),)
    assert _named_callers("compose_m7_execution_stage") == (Path("main.py"),)
    assert _named_callers("TopologyScheduler") == (
        Path("application/analysis_runs/topology_driven_workflow.py"),
    )


def test_m5_boundary_does_not_complete_or_fabricate_a_run_result() -> None:
    workflow_sources = (
        APP_ROOT / "application/analysis_runs/query_topology_workflow.py",
        APP_ROOT / "application/analysis_runs/topology_driven_workflow.py",
        APP_ROOT / "infrastructure/runtime/m7.py",
    )

    assert not any(
        _calls_name(source_path, call)
        for source_path in workflow_sources
        for call in ("complete_run", "TopologyRunResult")
    )
    assert any(
        _calls_name(source_path, "AwaitRunSummaryBoundary")
        for source_path in workflow_sources
    )
