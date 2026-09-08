import ast
import importlib.util
import sys
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2] / "app"
RUNTIME = "app.application.analysis_sessions.runtime"


def in_package(name: str, package: str) -> bool:
    return name == package or name.startswith(package + ".")


def dependency_violations(app_root: Path) -> list[str]:
    violations = []
    for layer in ("api", "application"):
        packages = ("app.domain", "app.application", "app.api" if layer == "api" else "app.ports")
        external = {"pydantic", "fastapi", "starlette"} if layer == "api" else {"pydantic"}
        for path in (app_root / layer).rglob("*.py"):
            relative = path.relative_to(app_root)
            package = ".".join(("app", *relative.parent.parts))
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=path)
            for node in ast.walk(tree):
                imports = []
                if isinstance(node, ast.Import):
                    imports = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    base = node.module or ""
                    if node.level:
                        base = importlib.util.resolve_name("." * node.level + base, package)
                    imports = [base if alias.name == "*" else f"{base}.{alias.name}" for alias in node.names]
                for name in imports:
                    root = name.split(".", 1)[0]
                    allowed = (
                        root in sys.stdlib_module_names or root in external
                        or any(in_package(name, item) for item in packages)
                    )
                    if in_package(name, RUNTIME):
                        allowed = relative.as_posix() == "application/analysis_sessions/coordinator.py"
                    if not allowed:
                        violations.append(f"{relative}:{node.lineno}: forbidden import {name}")
                if layer != "api":
                    continue
                target, attribute = None, ""
                if isinstance(node, ast.Attribute):
                    target, attribute = node.value, node.attr
                elif (
                    isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in {"getattr", "setattr", "delattr"}
                    and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)
                ):
                    target, attribute = node.args[0], node.args[1].value
                if (
                    attribute.startswith("_") and not attribute.startswith("__")
                    and not (isinstance(target, ast.Name) and target.id == "self")
                ):
                    violations.append(f"{relative}:{node.lineno}: API accesses private {attribute}")
    return violations


def test_api_and_application_respect_dependency_and_runtime_boundaries() -> None:
    violations = dependency_violations(APP_ROOT)
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize(("path", "source", "allowed"), [
    ("application/service.py", "from fastapi import Request as RequestAlias", False),
    ("application/service.py", "import starlette.responses as responses", False),
    ("application/service.py", "from app.infrastructure.config import AppSettings", False),
    ("application/service.py", "from app.api.schemas import analysis_sessions", False),
    ("application/analysis_sessions/workflow.py", "from .runtime import SessionRuntime", False),
    ("api/http/route.py", "from ...application.analysis_sessions import runtime as state", False),
    ("api/http/route.py", "from app.infrastructure.runtime.system_clock import SystemClock", False),
    ("api/http/route.py", "session = coordinator._sessions[session_id]", False),
    ("api/http/route.py", "getattr(coordinator, '_lookup')(session_id)", False),
    ("api/http/route.py", "self._coordinator._sessions.clear()", False),
    ("application/service.py", "from app.ports.clock import ClockPort\nfrom pydantic import TypeAdapter", True),
    ("api/http/route.py", "from ...application.analysis_sessions.models import LifecycleEvent\nself._frames.close()", True),
    ("api/http/route.py", "getattr(request.state, 'session_id', None)\nerror.__traceback__", True),
])
def test_dependency_gate_detects_aliases_relative_imports_and_private_access(tmp_path, path, source, allowed):
    app_root = tmp_path / "app"
    source_path = app_root / path
    source_path.parent.mkdir(parents=True)
    source_path.write_text(source, encoding="utf-8")
    assert bool(dependency_violations(app_root)) is not allowed
