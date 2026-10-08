import ast
import tomllib
from pathlib import Path

import pytest

from app.application.evidence_tools import create_evidence_tool_registry
from app.infrastructure.config import AppSettings
from app.infrastructure.config import load_parameter_capability_registry
from app.main import create_app


BACKEND_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = BACKEND_ROOT / "app"
M8_APPLICATION_PACKAGE = "app.application.transcript_memory"
M8_DOMAIN_PATHS = (
    Path("domain/evidence/evidence_tool_options.py"),
    Path("domain/evidence/historical_case_source.py"),
    Path("domain/evidence/historical_inference_payload.py"),
    Path("domain/evidence/historical_supporting_fragment_projection.py"),
    Path("domain/evidence/historical_supporting_fragment_reference.py"),
    Path("domain/evidence/historical_transcript_record.py"),
    Path("domain/evidence/transcript_evidence_request.py"),
    Path("domain/model_capability.py"),
    Path("domain/model_catalog_snapshot.py"),
    Path("domain/model_role.py"),
)
M8_DOMAIN_ROOTS = (Path("domain/memory_rag"),)
M8_PORT_PATHS = (
    Path("ports/embedding.py"),
    Path("ports/historical_sources.py"),
    Path("ports/rag_index.py"),
    Path("ports/rag_jobs.py"),
    Path("ports/transcript_evidence.py"),
)
FORBIDDEN_M8_LAYER_IMPORTS = (
    "fastapi",
    "sqlalchemy",
    "pgvector",
    "openai",
    "anthropic",
    "app.api",
    "app.infrastructure",
)
EXTERNAL_VECTOR_PACKAGES = {
    "chroma",
    "chromadb",
    "faiss",
    "milvus",
    "pinecone",
    "qdrant-client",
    "qdrant_client",
    "weaviate-client",
    "weaviate",
}
DIRECT_MEMORY_PORTS = (
    "app.ports.embedding",
    "app.ports.historical_sources",
    "app.ports.rag_index",
    "app.ports.rag_jobs",
)
TRANSCRIPT_SEARCH_PORT = "app.ports.transcript_evidence"
DIRECT_MEMORY_SYMBOLS = {
    "EmbeddingPort",
    "HistoricalSourceReaderPort",
    "RAGIndexPort",
    "RAGJobStorePort",
}
TRANSCRIPT_SEARCH_SYMBOL = "TranscriptEvidencePort"
FORBIDDEN_DIRECT_CALLER_ROOTS = (
    Path("api"),
    Path("application/analysis_runs"),
    Path("application/query_understanding"),
    Path("application/reasoning"),
    Path("application/run_summary"),
)
RESULT_BODY_NAMES = {
    "answer",
    "answer_body",
    "fragment_body",
    "result_body",
    "source_payload",
    "summary",
    "transcript",
}


def _source_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=path)


def _imported_modules(path: Path) -> tuple[tuple[int, str], ...]:
    imports: list[tuple[int, str]] = []
    for node in ast.walk(_source_tree(path)):
        if isinstance(node, ast.Import):
            imports.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.extend(
                (
                    node.lineno,
                    (
                        node.module
                        if alias.name == "*"
                        else f"{node.module}.{alias.name}"
                    ),
                )
                for alias in node.names
            )
    return tuple(imports)


def _matches_module(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def _m8_layer_violations(app_root: Path) -> tuple[str, ...]:
    sources = [
        app_root / relative
        for relative in (*M8_DOMAIN_PATHS, *M8_PORT_PATHS)
        if (app_root / relative).is_file()
    ]
    for relative in M8_DOMAIN_ROOTS:
        domain_root = app_root / relative
        if domain_root.is_dir():
            sources.extend(domain_root.rglob("*.py"))
    memory_root = app_root / "application" / "transcript_memory"
    if memory_root.is_dir():
        sources.extend(memory_root.rglob("*.py"))
    violations: list[str] = []
    for source_path in sorted(set(sources)):
        for line_number, module in _imported_modules(source_path):
            if any(
                _matches_module(module, forbidden)
                for forbidden in FORBIDDEN_M8_LAYER_IMPORTS
            ):
                violations.append(
                    f"{source_path.relative_to(app_root)}:{line_number}: "
                    f"forbidden M8 layer import {module}"
                )
        source = source_path.read_text(encoding="utf-8")
        for forbidden in (
            "backend" + "_bk",
            "frontend" + "_bk",
            "app.m9",
            "app.application.m9",
            "app.infrastructure.m9",
        ):
            if forbidden in source:
                violations.append(
                    f"{source_path.relative_to(app_root)}: "
                    f"forbidden deferred/archive reference {forbidden}"
                )
    return tuple(violations)


def _direct_memory_caller_violations(app_root: Path) -> tuple[str, ...]:
    violations: list[str] = []
    for relative_root in FORBIDDEN_DIRECT_CALLER_ROOTS:
        root = app_root / relative_root
        if not root.is_dir():
            continue
        for source_path in sorted(root.rglob("*.py")):
            for line_number, module in _imported_modules(source_path):
                if _matches_module(module, M8_APPLICATION_PACKAGE) or any(
                    _matches_module(module, port) for port in DIRECT_MEMORY_PORTS
                ) or _matches_module(module, TRANSCRIPT_SEARCH_PORT) or (
                    module.rsplit(".", 1)[-1]
                    in {*DIRECT_MEMORY_SYMBOLS, TRANSCRIPT_SEARCH_SYMBOL}
                    and module.startswith("app.ports.")
                ):
                    violations.append(
                        f"{source_path.relative_to(app_root)}:{line_number}: "
                        f"direct memory/search dependency {module}"
                    )
    evidence_root = app_root / "application" / "evidence_tools"
    if evidence_root.is_dir():
        for source_path in sorted(evidence_root.rglob("*.py")):
            for line_number, module in _imported_modules(source_path):
                if _matches_module(module, M8_APPLICATION_PACKAGE) or any(
                    _matches_module(module, port) for port in DIRECT_MEMORY_PORTS
                ) or (
                    module.rsplit(".", 1)[-1] in DIRECT_MEMORY_SYMBOLS
                    and module.startswith("app.ports.")
                ):
                    violations.append(
                        f"{source_path.relative_to(app_root)}:{line_number}: "
                        "M5 must call the injected TranscriptEvidencePort, not M8 internals"
                    )
    return tuple(violations)


def _external_vector_violations(backend_root: Path) -> tuple[str, ...]:
    configuration = tomllib.loads(
        (backend_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    project = configuration["project"]
    dependencies = [*project.get("dependencies", ())]
    for group in project.get("optional-dependencies", {}).values():
        dependencies.extend(group)
    violations = [
        f"pyproject.toml: external vector dependency {dependency}"
        for dependency in dependencies
        if dependency.split("[", 1)[0].split("=", 1)[0].split("<", 1)[0]
        .split(">", 1)[0]
        .strip()
        .casefold()
        in EXTERNAL_VECTOR_PACKAGES
    ]
    app_root = backend_root / "app"
    if app_root.is_dir():
        for source_path in sorted(app_root.rglob("*.py")):
            for line_number, module in _imported_modules(source_path):
                if module.split(".", 1)[0].casefold() in EXTERNAL_VECTOR_PACKAGES:
                    violations.append(
                        f"{source_path.relative_to(backend_root)}:{line_number}: "
                        f"external vector import {module}"
                    )
    return tuple(violations)


def _embedded_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _result_body_embedding_violations(memory_root: Path) -> tuple[str, ...]:
    if not memory_root.is_dir():
        return ()
    violations: list[str] = []
    for source_path in sorted(memory_root.rglob("*.py")):
        for node in ast.walk(_source_tree(source_path)):
            if not isinstance(node, ast.Call):
                continue
            call_name = _embedded_name(node.func)
            if call_name not in {"embed", "embed_many", "embed_texts"}:
                continue
            argument_names = {
                name.casefold()
                for name in (
                    *(_embedded_name(argument) for argument in node.args),
                    *(_embedded_name(keyword.value) for keyword in node.keywords),
                )
                if name is not None
            }
            forbidden = argument_names & RESULT_BODY_NAMES
            if forbidden:
                violations.append(
                    f"{source_path.relative_to(memory_root)}:{node.lineno}: "
                    f"embeds result/source content {sorted(forbidden)}"
                )
    return tuple(violations)


def test_m8_domain_application_and_ports_keep_provider_and_orm_details_out() -> None:
    assert _m8_layer_violations(APP_ROOT) == ()


def test_only_the_injected_m5_producer_can_enter_transcript_memory_search() -> None:
    assert _direct_memory_caller_violations(APP_ROOT) == ()


def test_m8_uses_postgresql_pgvector_without_an_external_vector_database() -> None:
    assert _external_vector_violations(BACKEND_ROOT) == ()


def test_m8_has_no_result_body_embedding_shortcut() -> None:
    assert _result_body_embedding_violations(
        APP_ROOT / "application" / "transcript_memory"
    ) == ()


def test_m8_does_not_add_a_direct_route_or_third_sse() -> None:
    application = create_app(AppSettings(_env_file=None))
    paths = set(application.openapi()["paths"])

    assert {path for path in paths if path.endswith("/events")} == {
        "/api/analysis/runs/{run_id}/events",
        "/api/analysis/runs/{run_id}/sessions/{session_id}/events",
    }
    assert not any(
        forbidden in path.casefold()
        for path in paths if path.startswith("/api/analysis/")
        for forbidden in (
            "/rag",
            "/memory",
            "/embeddings",
            "/index/jobs",
            "/reindex",
        )
    )


def test_m8_keeps_one_v1_read_union_tool_and_no_feature_toggle() -> None:
    type_paths = [
        *(APP_ROOT / "domain" / "memory_rag").glob("*.py"),
        *(APP_ROOT / "domain" / "evidence").glob("historical_*.py"),
    ]
    class_names = {
        node.name
        for path in type_paths
        for node in ast.walk(_source_tree(path))
        if isinstance(node, ast.ClassDef)
    }
    assert not {name for name in class_names if name.endswith("V2")}

    source_union = (
        APP_ROOT / "domain" / "evidence" / "source_record.py"
    ).read_text(encoding="utf-8")
    assert source_union.count("| HistoricalTranscriptRecord") == 1
    assert "HistoricalTranscriptRecordV2" not in source_union

    registry = create_evidence_tool_registry(load_parameter_capability_registry())
    historical_tools = {
        descriptor.name
        for descriptor in registry.descriptors
        if "historical" in descriptor.name or "transcript" in descriptor.name
    }
    assert historical_tools == {"search_transcript_evidence"}
    assert not {
        name
        for name in AppSettings.model_fields
        if "enable" in name and ("rag" in name or "memory" in name)
    }


def test_production_composes_one_m8_owner_before_m5_m6_m7_and_worker_start() -> None:
    source = (APP_ROOT / "main.py").read_text(encoding="utf-8")
    order = tuple(
        source.index(marker)
        for marker in (
            "active_m8_builder(",
            "build_m5_runtime(",
            "active_m6_builder(",
            "bind_execution_stage(",
            "await m8_runtime.start()",
        )
    )

    assert order == tuple(sorted(order))
    assert source.count("transcript_port=m8_runtime.producer") == 1
    runtime_sources = tuple((APP_ROOT / "infrastructure" / "runtime").glob("*.py"))
    combined = "\n".join(path.read_text(encoding="utf-8") for path in runtime_sources)
    assert combined.count("RAGIndexWorker(") == 1
    assert combined.count("RAGSearchService(") == 1


def test_m8_layer_gate_detects_provider_orm_archive_and_deferred_imports(
    tmp_path: Path,
) -> None:
    app_root = tmp_path / "app"
    memory_root = app_root / "application" / "transcript_memory"
    memory_root.mkdir(parents=True)
    archive_name = "backend" + "_bk"
    (memory_root / "service.py").write_text(
        "import sqlalchemy\n"
        "from openai import AsyncOpenAI\n"
        "from app.infrastructure.persistence import records\n"
        f"ARCHIVE = '{archive_name}'\n"
        "DEFERRED = 'app.m9'\n",
        encoding="utf-8",
    )

    violations = _m8_layer_violations(app_root)

    assert len(violations) == 5


@pytest.mark.parametrize(
    "relative_path",
    (
        "api/http/rag.py",
        "application/query_understanding/parser.py",
        "application/reasoning/runner.py",
        "application/run_summary/runner.py",
        "application/analysis_runs/coordinator.py",
        "application/evidence_tools/executor.py",
    ),
)
def test_direct_caller_gate_detects_every_forbidden_owner(
    tmp_path: Path,
    relative_path: str,
) -> None:
    app_root = tmp_path / "app"
    source_path = app_root / relative_path
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        "from app.application.transcript_memory.search_service import SearchService\n",
        encoding="utf-8",
    )

    assert len(_direct_memory_caller_violations(app_root)) == 1


def test_external_vector_gate_detects_dependency_and_import(tmp_path: Path) -> None:
    backend_root = tmp_path / "backend"
    source_path = backend_root / "app" / "infrastructure" / "rag.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("import pinecone\n", encoding="utf-8")
    (backend_root / "pyproject.toml").write_text(
        "[project]\nname='fixture'\nversion='0'\ndependencies=['qdrant-client>=1']\n",
        encoding="utf-8",
    )

    violations = _external_vector_violations(backend_root)

    assert len(violations) == 2


def test_result_body_embedding_gate_detects_shortcut(tmp_path: Path) -> None:
    memory_root = tmp_path / "transcript_memory"
    memory_root.mkdir()
    (memory_root / "shortcut.py").write_text(
        "async def shortcut(port, result_body):\n"
        "    return await port.embed(result_body)\n",
        encoding="utf-8",
    )

    assert len(_result_body_embedding_violations(memory_root)) == 1
