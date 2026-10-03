import ast
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = BACKEND_ROOT / "app"
M7_HELPER = BACKEND_ROOT / "tests" / "application" / "reasoning" / "m7_support.py"
M7_SOURCE_PATHS = (
    Path("application/analysis_runs/query_topology_workflow.py"),
    Path("application/analysis_runs/topology_driven_workflow.py"),
    Path("application/analysis_runs/topology_scheduler.py"),
    Path("application/analysis_runs/topology_result_models.py"),
    Path("application/analysis_runs/topology_result_assembler.py"),
    Path("application/evidence_tools/upstream_result_store.py"),
    Path("application/evidence_tools/upstream_projection.py"),
    Path("infrastructure/runtime/m7.py"),
)
M8_IMPORTS = (
    "app.application.memory",
    "app.application.rag",
    "app.ports.rag_index",
    "app.ports.transcript_evidence",
)


def _source_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=path)


def _imported_modules(path: Path) -> tuple[tuple[int, str], ...]:
    imports = []
    for node in ast.walk(_source_tree(path)):
        if isinstance(node, ast.Import):
            imports.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append((node.lineno, node.module))
    return tuple(imports)


def _m7_sources(app_root: Path) -> tuple[Path, ...]:
    sources = {
        app_root / relative
        for relative in M7_SOURCE_PATHS
        if (app_root / relative).is_file()
    }
    summary_root = app_root / "application" / "run_summary"
    if summary_root.is_dir():
        sources.update(summary_root.rglob("*.py"))
    return tuple(sorted(sources))


def _m7_dependency_violations(app_root: Path) -> tuple[str, ...]:
    violations = []
    for source_path in _m7_sources(app_root):
        for line_number, module in _imported_modules(source_path):
            if any(
                module == forbidden or module.startswith(forbidden + ".")
                for forbidden in M8_IMPORTS
            ):
                violations.append(
                    f"{source_path.relative_to(app_root)}:{line_number}: "
                    f"M7 imports deferred M8 dependency {module}"
                )
    return tuple(violations)


def _named_calls(app_root: Path, name: str) -> tuple[Path, ...]:
    callers = []
    for source_path in sorted(app_root.rglob("*.py")):
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
                callers.append(source_path.relative_to(app_root))
    return tuple(callers)


def _owned_workflow_run_calls(app_root: Path) -> tuple[Path, ...]:
    callers = []
    for source_path in sorted(app_root.rglob("*.py")):
        for node in ast.walk(_source_tree(source_path)):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "_workflow"
            ):
                continue
            callers.append(source_path.relative_to(app_root))
    return tuple(callers)


def test_topology_workflow_has_one_composition_and_one_execution_owner() -> None:
    compositions = _named_calls(APP_ROOT, "QueryTopologyWorkflow")
    executions = _owned_workflow_run_calls(APP_ROOT)

    assert len(compositions) == 1
    assert compositions[0].parts[:2] == ("infrastructure", "runtime")
    assert executions == (Path("application/analysis_runs/coordinator.py"),)


def test_m7_sources_do_not_import_deferred_m8_dependencies() -> None:
    assert _m7_dependency_violations(APP_ROOT) == ()


def test_m7_dependency_gate_detects_transcript_rag_import(tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    source = app_root / M7_SOURCE_PATHS[1]
    source.parent.mkdir(parents=True)
    source.write_text(
        "from app.ports.transcript_evidence import TranscriptEvidencePort\n",
        encoding="utf-8",
    )

    assert len(_m7_dependency_violations(app_root)) == 1


def test_m7_reasoning_support_remains_a_test_only_helper() -> None:
    assert M7_HELPER.is_file()
    assert not any(path.name == "m7_support.py" for path in APP_ROOT.rglob("*.py"))
    assert all(
        "tests.application.reasoning.m7_support"
        not in source_path.read_text(encoding="utf-8")
        for source_path in APP_ROOT.rglob("*.py")
    )
