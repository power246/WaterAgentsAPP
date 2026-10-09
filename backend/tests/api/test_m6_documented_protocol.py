import json
import re
from pathlib import Path
from urllib.parse import unquote

from app.application.transcripts.registry import create_production_registry
from app.domain import RiskLevel
from tests.m4_support import create_test_app


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
M6_ROOT = REPOSITORY_ROOT / "docs" / "M6"
API_GUIDE = M6_ROOT / "m6_single_agent_reasoning_api.md"
IMPLEMENTATION_TODO = M6_ROOT / "m6_implementation_todo.md"
DEFERRED_CONTRACTS = REPOSITORY_ROOT / "docs" / "deferred_milestone_contracts.md"
AUDITED_DOCS = (
    *sorted(M6_ROOT.glob("*.md")),
    REPOSITORY_ROOT / "docs" / "backend_architecture_skeleton.md",
    REPOSITORY_ROOT / "docs" / "backend_refactor_milestones.md",
    DEFERRED_CONTRACTS,
    REPOSITORY_ROOT / "docs" / "frontend_integration_contracts.md",
)


def test_m6_json_examples_parse() -> None:
    blocks = re.findall(
        r"^```json\s*\n(.*?)^```\s*$",
        API_GUIDE.read_text(encoding="utf-8"),
        re.DOTALL | re.MULTILINE,
    )

    assert len(blocks) == 12
    assert all(isinstance(json.loads(block), dict) for block in blocks)


def test_m6_contract_matrix_covers_the_normative_contract_id_set() -> None:
    expected = {
        *(f"GLOBAL-C{index:02}" for index in range(1, 12)),
        *(f"M6-C{index:02}" for index in range(1, 20)),
        *(f"M6-A{index:02}" for index in range(1, 11)),
    }
    contracts = DEFERRED_CONTRACTS.read_text(encoding="utf-8")
    normative = set(
        re.findall(r"\*\*((?:GLOBAL|M6)-[CA]\d{2})\b", contracts)
    )
    assert expected <= normative
    assert {
        contract
        for contract in normative
        if contract.startswith(("GLOBAL-", "M6-"))
    } == expected

    todo = IMPLEMENTATION_TODO.read_text(encoding="utf-8")
    matrix = todo.split("## 契约覆盖矩阵", 1)[1].split(
        "## 实际完成记录格式", 1
    )[0]
    covered = set(re.findall(r"\b(?:GLOBAL|M6)-[CA]\d{2}\b", matrix))
    assert covered == expected


def test_m6_docs_have_balanced_fences_valid_links_and_no_trailing_space() -> None:
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


def test_runtime_registry_openapi_and_risk_levels_match_m6_docs() -> None:
    content_types = {
        "waterbody_agent_input",
        "waterbody_model_request",
        "waterbody_agent_message",
        "waterbody_model_response",
        "reasoning_decision",
        "inference_result",
    }
    registry = create_production_registry()
    assert content_types == {
        content_type
        for content_type, version in registry.writable_keys
        if content_type in content_types and version == 1
    }
    assert {level.value for level in RiskLevel} == {
        "none",
        "low",
        "medium",
        "high",
        "indeterminate",
    }

    paths = set(create_test_app().openapi()["paths"])
    assert {
        "/health",
        "/api/settings",
        "/api/settings/unit-policies/preview",
        "/api/settings/unit-policies",
        "/api/keys",
        "/api/keys/{key_name}",
        "/api/keys/{key_name}/test",
        "/api/analysis/runs",
        "/api/analysis/runs/{run_id}",
        "/api/analysis/runs/{run_id}/cancel",
        "/api/analysis/runs/{run_id}/events",
        "/api/analysis/runs/{run_id}/persistence/retry",
        "/api/analysis/runs/{run_id}/sessions/{session_id}",
        "/api/analysis/runs/{run_id}/sessions/{session_id}/events",
        "/api/analysis/runs/{run_id}/sessions/{session_id}/transcript",
        "/api/analysis/runs/{run_id}/transcript/{item_id}/content",
    } <= paths
    assert not any(
        forbidden in path
        for path in paths
        for forbidden in (
            "/__test__",
            "/reasoning",
            "/models/complete",
            "/tools",
            "/evidence/collect",
        )
    )


def test_m6_status_and_m7_m9_handoff_are_current() -> None:
    for document in sorted(M6_ROOT.glob("*.md")):
        header = "\n".join(document.read_text(encoding="utf-8").splitlines()[:8])
        assert "尚未实现" not in header
        assert "尚未开始 M6" not in header

    todo = IMPLEMENTATION_TODO.read_text(encoding="utf-8")
    assert "M7：`RunSingleWaterbodyAgent`" in todo
    assert "M8：现有 transcript typed contents" in todo
    assert "M9：轻量 `result_ref/result_summary`" in todo

    contracts = DEFERRED_CONTRACTS.read_text(encoding="utf-8")
    assert "### M6 待验收索引" in contracts
    assert "真实 OpenAI waterbody provider" in contracts
    assert "Ubuntu/macOS/Windows" in contracts
