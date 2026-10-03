import json
import re
from pathlib import Path
from urllib.parse import unquote

from app.application.analysis_runs.topology_result_models import (
    MAX_M7_DIRECT_UPSTREAM_NODES,
    MAX_M7_RESULT_CELL_REFS,
    MAX_M7_TOPOLOGY_NODES,
    TopologyRunResult,
    WaterbodyDependencyManifest,
)
from app.application.run_summary.models import RunSummaryInput, RunSummaryModelRequest
from app.application.transcripts.registry import create_production_registry
from tests.m4_support import create_test_app


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
M7_ROOT = REPOSITORY_ROOT / "docs" / "M7"
API_GUIDE = M7_ROOT / "m7_topology_driven_multi_agent_api.md"
IMPLEMENTATION_TODO = M7_ROOT / "m7_implementation_todo.md"
DEFERRED_CONTRACTS = REPOSITORY_ROOT / "docs" / "deferred_milestone_contracts.md"
CI_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "backend-m1.yml"
AUDITED_DOCS = (
    *sorted(M7_ROOT.glob("*.md")),
    REPOSITORY_ROOT / "docs" / "backend_architecture_skeleton.md",
    REPOSITORY_ROOT / "docs" / "backend_refactor_milestones.md",
    DEFERRED_CONTRACTS,
    REPOSITORY_ROOT / "docs" / "frontend_integration_contracts.md",
)


def _property_schema(model: type, *path: str) -> dict:
    schema = model.model_json_schema()
    current = schema
    for segment in path:
        if "$ref" in current:
            current = schema["$defs"][current["$ref"].rsplit("/", 1)[-1]]
        current = current["properties"][segment]
    return current


def test_m7_json_examples_parse() -> None:
    blocks = re.findall(
        r"^```json\s*\n(.*?)^```\s*$",
        API_GUIDE.read_text(encoding="utf-8"),
        re.DOTALL | re.MULTILINE,
    )

    assert len(blocks) == 7
    assert all(isinstance(json.loads(block), dict) for block in blocks)


def test_m7_contract_matrix_covers_the_normative_contract_id_set() -> None:
    expected = {
        *(f"GLOBAL-C{index:02}" for index in range(1, 12)),
        *(f"M7-C{index:02}" for index in range(1, 13)),
        *(f"M7-A{index:02}" for index in range(1, 8)),
    }
    contracts = DEFERRED_CONTRACTS.read_text(encoding="utf-8")
    normative = set(re.findall(r"\*\*((?:GLOBAL|M7)-[CA]\d{2})\b", contracts))
    assert expected <= normative
    assert {
        contract
        for contract in normative
        if contract.startswith(("GLOBAL-", "M7-"))
    } == expected

    todo = IMPLEMENTATION_TODO.read_text(encoding="utf-8")
    matrix = todo.split("## 契约覆盖矩阵", 1)[1].split(
        "## 实际完成记录格式", 1
    )[0]
    covered = set(re.findall(r"\b(?:GLOBAL|M7)-[CA]\d{2}\b", matrix))
    assert covered == expected


def test_m7_docs_have_balanced_fences_valid_links_and_no_trailing_space() -> None:
    link_pattern = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
    for document in AUDITED_DOCS:
        text = document.read_text(encoding="utf-8")
        assert sum(line.startswith("```") for line in text.splitlines()) % 2 == 0
        assert not any(line.endswith((" ", "\t")) for line in text.splitlines())
        for raw_target in link_pattern.findall(text):
            target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
            if not target or target.startswith(("#", "http://", "https://")):
                continue
            path_text = unquote(target.split("#", 1)[0])
            assert (document.parent / path_text).resolve().exists(), (
                document,
                target,
            )


def test_runtime_registry_and_openapi_match_the_m7_contract() -> None:
    m7_content_types = {
        "waterbody_dependency_manifest",
        "run_summary_input",
        "run_summary_model_request",
        "run_summary_agent_message",
        "run_summary_model_response",
        "run_summary_result",
        "topology_run_result",
    }
    registry = create_production_registry()
    assert m7_content_types == {
        content_type
        for content_type, version in registry.writable_keys
        if content_type in m7_content_types and version == 1
    }

    paths = set(create_test_app().openapi()["paths"])
    assert paths == {
        "/health",
        "/api/analysis/runs",
        "/api/analysis/runs/{run_id}",
        "/api/analysis/runs/{run_id}/cancel",
        "/api/analysis/runs/{run_id}/events",
        "/api/analysis/runs/{run_id}/persistence/retry",
        "/api/analysis/runs/{run_id}/sessions/{session_id}",
        "/api/analysis/runs/{run_id}/sessions/{session_id}/events",
        "/api/analysis/runs/{run_id}/sessions/{session_id}/transcript",
        "/api/analysis/runs/{run_id}/transcript/{item_id}/content",
    }
    assert not any(
        forbidden in path
        for path in paths
        for forbidden in (
            "/__test__",
            "/summary",
            "/models/complete",
            "/tools",
            "/reasoning",
        )
    )


def test_m7_collection_schemas_have_explicit_topology_bounds() -> None:
    assert _property_schema(
        WaterbodyDependencyManifest, "dependencies"
    )["maxItems"] == MAX_M7_DIRECT_UPSTREAM_NODES
    assert _property_schema(TopologyRunResult, "node_outcomes")[
        "maxItems"
    ] == MAX_M7_TOPOLOGY_NODES
    assert _property_schema(RunSummaryInput, "report_cards")[
        "maxItems"
    ] == MAX_M7_DIRECT_UPSTREAM_NODES
    assert _property_schema(RunSummaryInput, "allowed_source_refs")[
        "maxItems"
    ] == 32_768
    assert _property_schema(RunSummaryModelRequest, "messages")["maxItems"] == 8
    assert MAX_M7_RESULT_CELL_REFS == 2_048


def test_m7_status_ci_and_m8_m9_handoff_are_current() -> None:
    api = API_GUIDE.read_text(encoding="utf-8")
    assert "/api/analysis/runs/{run_id}/status" not in api

    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    assert (
        "uv run --project backend --locked python "
        "backend/scripts/m7_topology_workflow_smoke.py"
    ) in workflow

    todo = IMPLEMENTATION_TODO.read_text(encoding="utf-8")
    assert "## M8 / M9 接力出口" in todo
    assert "- M8：" in todo
    assert "- M9：" in todo
    assert "M8 自己建立 index/search producer" in todo
    assert "M9 自己实现页面" in todo

    contracts = DEFERRED_CONTRACTS.read_text(encoding="utf-8")
    assert "### M7 待验收索引" in contracts
    assert "真实 provider" in contracts
    assert "Ubuntu/macOS/Windows" in contracts
