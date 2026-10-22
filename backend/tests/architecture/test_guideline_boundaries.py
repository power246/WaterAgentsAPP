import ast
from pathlib import Path

import pytest

from app.application.reasoning.working_context import require_safe_prompt_text
from app.domain.guidelines import project_model_view
from tests.guideline_support import content

APP_ROOT = Path(__file__).resolve().parents[2] / "app"


def _violations(paths, *, domain=True):
    violations = []
    forbidden_symbols = {
        "CanonicalCaseQueryV1", "EmbeddingProfile", "EmbeddingPort", "HistoricalInferencePayload",
        "RAGCaseProjectionV1", "EvidenceFragment", "EvidenceBundle",
    }
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
                if any(alias.name in forbidden_symbols for alias in node.names):
                    violations.append(f"{path.name}:{node.lineno}: case/evidence contract import")
            else:
                continue
            for module in modules:
                if (domain and module.startswith(("app.application", "app.infrastructure", "app.api"))) or (
                    module.startswith("app.domain.memory_rag")
                    and module != "app.domain.memory_rag.embedding_vector"
                ) or module.startswith("app.application.transcript_memory"):
                    violations.append(f"{path.name}:{node.lineno}: forbidden owner {module}")
    return violations


def test_guideline_domain_and_ports_do_not_adopt_case_or_observation_semantics():
    paths = [*(APP_ROOT / "domain/guidelines").glob("*.py"), APP_ROOT / "ports/guidelines.py"]
    assert _violations(paths) == []


def test_guideline_application_adapter_and_runtime_do_not_adopt_historical_case_contracts():
    paths = [*(APP_ROOT / "application/guidelines").glob("*.py"),
             APP_ROOT / "infrastructure/adapters/openai_guideline_embeddings.py",
             APP_ROOT / "infrastructure/adapters/openai_embedding_transport.py",
             APP_ROOT / "infrastructure/runtime/guidelines.py"]
    assert _violations(paths, domain=False) == []


@pytest.mark.parametrize("source", [
    "from app.domain import CanonicalCaseQueryV1 as Alias\n",
    "from app.domain.memory_rag.embedding_profile import EmbeddingProfile\n",
    "from app.domain import EvidenceFragment\n",
    "from app.application.transcript_memory import search\n",
    "import app.infrastructure.persistence.schema\n",
])
def test_isolation_guard_detects_wrong_namespace_or_role(tmp_path, source):
    path = tmp_path / "contaminated.py"
    path.write_text(source, encoding="utf-8")
    assert _violations([path])


def test_complete_model_view_has_its_own_limit_and_old_summary_guard_stays_strict():
    original = content(body_markdown="If A, retain B; otherwise C.\n" * 100)
    assert project_model_view(original).body == original.body_markdown
    assert len(original.body_markdown) > 2048
    for text in (original.body_markdown, "https://source.invalid/", "api_key=fixture"):
        with pytest.raises(ValueError):
            require_safe_prompt_text(text)
