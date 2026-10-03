import ast
import inspect
from pathlib import Path

from app.application.evidence_tools import RunLocalUpstreamResultStore
from app.ports import UpstreamResultPort


BACKEND_ROOT = Path(__file__).resolve().parents[2]
M2_SOURCES = (
    BACKEND_ROOT / "app/application/evidence_tools/upstream_result_store.py",
    BACKEND_ROOT / "app/application/evidence_tools/upstream_projection.py",
)
FORBIDDEN_IMPORTS = (
    "app.application.analysis_runs.coordinator",
    "app.application.analysis_runs.topology_scheduler",
    "app.application.reasoning.runner",
    "app.application.run_summary",
    "app.infrastructure.runtime",
)
FORBIDDEN_CALLS = {
    "create_agent_session",
    "complete_waterbody_agent_session",
    "run_waterbody_agent",
    "run_summarizer",
}


def test_upstream_store_matches_the_existing_m5_port_signature() -> None:
    assert tuple(inspect.signature(RunLocalUpstreamResultStore.collect).parameters) == (
        "self",
        "request",
        "deadline",
    )
    assert tuple(inspect.signature(UpstreamResultPort.collect).parameters) == (
        "self",
        "request",
        "deadline",
    )


def test_m2_sources_do_not_create_sessions_run_agents_or_bind_runtime() -> None:
    violations = []
    for path in M2_SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = (node.module,)
            else:
                modules = ()
            for module in modules:
                if any(
                    module == forbidden or module.startswith(forbidden + ".")
                    for forbidden in FORBIDDEN_IMPORTS
                ):
                    violations.append(f"{path.name}:{node.lineno}: import {module}")
            if isinstance(node, ast.Call):
                name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else None
                )
                if name in FORBIDDEN_CALLS:
                    violations.append(f"{path.name}:{node.lineno}: call {name}")
    assert violations == []
