"""M9 guardrails that apply before its production routes and frontend exist."""

import ast
import json
from pathlib import Path

from app.infrastructure.config import AppSettings
from app.infrastructure.persistence.schema import metadata
from app.main import create_app


ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = ROOT / "backend" / "app"
ARCHIVE_NAMES = ("backend" + "_bk", "frontend" + "_bk")
FORBIDDEN_M9_IMPORTS = (
    "fastapi", "starlette", "sqlalchemy", "keyring", "react",
    "app.api", "app.infrastructure",
)
FORBIDDEN_DEPENDENCIES = ("celery", "redis", "rq", "electron", "tauri")


def _imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    return tuple(imports)


def _matches(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def _source_violations(app_root: Path) -> tuple[str, ...]:
    violations = []
    for path in sorted(app_root.rglob("*.py")):
        relative = path.relative_to(app_root).as_posix()
        source = path.read_text(encoding="utf-8")
        for archive in ARCHIVE_NAMES:
            if archive in source:
                violations.append(f"{relative}: archive reference {archive}")
        imports = _imports(path)
        in_m9_layer = relative in {
            "domain/settings_admin.py", "application/settings_admin.py",
        } or relative.startswith((
            "domain/settings_admin/", "application/settings_admin/",
        ))
        if in_m9_layer:
            for name in imports:
                if any(_matches(name, item) for item in FORBIDDEN_M9_IMPORTS):
                    violations.append(f"{relative}: forbidden layer import {name}")
        if relative.startswith("api/http/"):
            for name in imports:
                if any(_matches(name, item) for item in (
                    "app.infrastructure", "sqlalchemy", "keyring",
                )):
                    violations.append(f"{relative}: route owns infrastructure {name}")
            for node in ast.walk(ast.parse(source, filename=str(path))):
                if isinstance(node, ast.Attribute) and node.attr in {
                    "engine", "key_store", "rag_repository", "settings_store",
                    "transcript_store",
                } and isinstance(node.value, ast.Attribute) and node.value.attr == "state":
                    violations.append(f"{relative}: route accesses {node.attr} directly")
    return tuple(violations)


def test_production_source_keeps_archive_and_m9_layer_boundaries() -> None:
    assert _source_violations(APP_ROOT) == ()
    extra_sources = [
        *(ROOT / "backend" / "scripts").glob("*.py"),
        *(ROOT / ".github" / "workflows").glob("*.yml"),
        *(ROOT / "frontend" / "src").rglob("*.ts"),
        *(ROOT / "frontend" / "src").rglob("*.tsx"),
        ROOT / "frontend" / "package.json",
        ROOT / "frontend" / "vite.config.ts",
    ]
    assert not [
        str(path.relative_to(ROOT))
        for path in extra_sources if path.is_file()
        if any(name in path.read_text(encoding="utf-8") for name in ARCHIVE_NAMES)
    ]


def test_source_gate_rejects_archive_layer_and_route_violations(tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    files = {
        "domain/settings_admin/models.py": "import keyring\n",
        "application/settings_admin/service.py": "from fastapi import Request\n",
        "api/http/admin.py": "from app.infrastructure.persistence import schema\n",
        "api/http/keys.py": "def route(request):\n    return request.app.state.key_store\n",
        "infrastructure/web/frontend.py": "PATH = 'frontend" + "_bk/dist'\n",
    }
    for relative, source in files.items():
        path = app_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    violations = _source_violations(app_root)
    assert len(violations) == 5


def test_m9_surface_keeps_two_sse_routes_and_no_direct_or_desktop_api() -> None:
    application = create_app(AppSettings(_env_file=None))
    paths = set(application.openapi()["paths"])
    assert {path for path in paths if path.endswith("/events")} == {
        "/api/analysis/runs/{run_id}/events",
        "/api/analysis/runs/{run_id}/sessions/{session_id}/events",
    }
    sse_paths = {
        path for path, operations in application.openapi()["paths"].items()
        if any(
            "text/event-stream" in response.get("content", {})
            for operation in operations.values()
            for response in operation.get("responses", {}).values()
        )
    }
    assert sse_paths == {
        "/api/analysis/runs/{run_id}/events",
        "/api/analysis/runs/{run_id}/sessions/{session_id}/events",
    }
    assert not any(
        token in path.casefold()
        for path in paths
        for token in (
            "/ws", "/websocket", "/socket", "/evidence-cache",
            "/rag/search", "/auth", "/login", "/tools/", "/models/",
        )
    )
    assert not any("websocket" in route.__class__.__name__.casefold()
                   for route in application.routes)


def test_database_and_dependencies_do_not_gain_secret_or_queue_storage() -> None:
    assert AppSettings.model_fields["host"].default == "127.0.0.1"
    assert "workers=1" in (ROOT / "backend" / "app" / "__main__.py").read_text(
        encoding="utf-8"
    )
    assert not any(
        token in table.name.casefold()
        for table in metadata.tables.values()
        for token in ("credential", "secret", "api_key", "evidence_cache")
    )
    assert not any(
        token in column.name.casefold()
        for table in metadata.tables.values()
        for column in table.columns
        for token in ("credential", "secret", "api_key", "ciphertext")
    )
    backend_dependencies = (ROOT / "backend" / "pyproject.toml").read_text(
        encoding="utf-8"
    ).casefold()
    assert not any(f'"{name}' in backend_dependencies for name in FORBIDDEN_DEPENDENCIES)
    frontend_manifest = ROOT / "frontend" / "package.json"
    if frontend_manifest.is_file():
        manifest = json.loads(frontend_manifest.read_text(encoding="utf-8"))
        dependencies = set(manifest.get("dependencies", {})) | set(
            manifest.get("devDependencies", {})
        )
        assert not dependencies.intersection(FORBIDDEN_DEPENDENCIES)
        assert not any(
            archive in json.dumps(manifest) for archive in ARCHIVE_NAMES
        )
