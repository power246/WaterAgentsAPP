import ast
import importlib.util
from pathlib import Path

from app.infrastructure.config import AppSettings
from app.main import create_app


BACKEND_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = BACKEND_ROOT / "app"


def _module_name(source_path: Path, app_root: Path) -> str:
    relative = source_path.relative_to(app_root).with_suffix("")
    parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
    return ".".join(("app", *parts))


def _is_test_only_module(module_name: str) -> bool:
    parts = module_name.split(".")
    return any(
        part in {"test", "tests"}
        or part.startswith("test_")
        or part.endswith("_test")
        for part in parts
    )


def _import_targets(
    node: ast.Import | ast.ImportFrom,
    *,
    package: str,
) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    base = node.module or ""
    if node.level:
        base = importlib.util.resolve_name(f"{'.' * node.level}{base}", package)
    return tuple(
        base if alias.name == "*" else f"{base}.{alias.name}"
        for alias in node.names
    )


def production_test_import_violations(app_root: Path) -> tuple[str, ...]:
    violations: list[str] = []
    for source_path in sorted(app_root.rglob("*.py")):
        source_module = _module_name(source_path, app_root)
        if _is_test_only_module(source_module):
            continue
        package = (
            source_module
            if source_path.name == "__init__.py"
            else source_module.rpartition(".")[0]
        )
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=source_path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                targets = _import_targets(node, package=package)
            elif (
                isinstance(node, ast.Call)
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and (
                    (isinstance(node.func, ast.Name) and node.func.id == "__import__")
                    or (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr == "import_module"
                    )
                )
            ):
                targets = (node.args[0].value,)
            else:
                continue
            violations.extend(
                f"{source_path.relative_to(app_root)}:{node.lineno}: "
                f"production imports test-only module {target}"
                for target in targets
                if _is_test_only_module(target)
            )
    return tuple(violations)


def _class_definitions(app_root: Path, class_name: str) -> tuple[Path, ...]:
    definitions = []
    for source_path in sorted(app_root.rglob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=source_path)
        if any(
            isinstance(node, ast.ClassDef) and node.name == class_name
            for node in tree.body
        ):
            definitions.append(source_path.relative_to(app_root))
    return tuple(definitions)


def test_production_import_graph_excludes_test_only_modules() -> None:
    assert production_test_import_violations(APP_ROOT) == ()


def test_production_import_gate_handles_relative_and_dynamic_imports(
    tmp_path: Path,
) -> None:
    app_root = tmp_path / "app"
    runtime = app_root / "infrastructure" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "m5_test.py").write_text(
        "from .m5 import build_runtime\n",
        encoding="utf-8",
    )
    production = runtime / "m5.py"
    production.write_text(
        "from . import m5_test\n"
        "import importlib\n"
        "importlib.import_module('app.api.http.m5_test')\n",
        encoding="utf-8",
    )

    violations = production_test_import_violations(app_root)

    assert len(violations) == 2
    assert all("m5_test" in violation for violation in violations)


def test_test_only_modules_may_reuse_production_modules(tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    runtime = app_root / "infrastructure" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "m5.py").write_text("VALUE = 1\n", encoding="utf-8")
    (runtime / "m5_test.py").write_text(
        "from .m5 import VALUE\n",
        encoding="utf-8",
    )

    assert production_test_import_violations(app_root) == ()


def test_transcript_has_one_writer_and_one_reader_implementation() -> None:
    assert _class_definitions(APP_ROOT, "TranscriptWriter") == (
        Path("application/analysis_runs/writer.py"),
    )
    assert _class_definitions(APP_ROOT, "TranscriptReader") == (
        Path("application/transcripts/reader.py"),
    )


def test_production_exposes_exactly_the_two_existing_sse_routes() -> None:
    application = create_app(AppSettings(_env_file=None))
    event_paths = {
        path
        for path in application.openapi()["paths"]
        if path.endswith("/events")
    }

    assert event_paths == {
        "/api/analysis/runs/{run_id}/events",
        "/api/analysis/runs/{run_id}/sessions/{session_id}/events",
    }
