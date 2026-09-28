import ast
from pathlib import Path

from app.application.hydrology_topology import HydrologyGraphBuilder

BACKEND = Path(__file__).resolve().parents[2]
BUILDER = BACKEND / "app" / "application" / "hydrology_topology" / "builder.py"


def test_topology_builder_has_no_transport_persistence_or_agent_session_dependency() -> None:
    tree = ast.parse(BUILDER.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )

    assert not any(
        name.startswith(
            (
                "app.api",
                "app.infrastructure",
                "app.application.agent_sessions",
                "app.application.transcripts",
            )
        )
        for name in imported
    )
    assert "session" not in HydrologyGraphBuilder.__init__.__annotations__
