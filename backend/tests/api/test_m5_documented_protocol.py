import json
import re
from pathlib import Path
from urllib.parse import unquote

from app.application.evidence_tools import create_evidence_tool_registry
from app.application.transcripts.registry import create_production_registry
from app.domain import (
    EXPECTED_PARAMETER_CAPABILITY_COUNT,
    EvidenceBundle,
    EvidenceFragment,
    EvidenceSourceRecords,
    EvidenceToolCall,
    EvidenceToolResult,
)
from app.infrastructure.config import load_parameter_capability_registry
from tests.m4_support import create_test_app


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
M5_ROOT = REPOSITORY_ROOT / "docs" / "M5"
API_GUIDE = M5_ROOT / "m5_evidence_tools_api.md"
IMPLEMENTATION_TODO = M5_ROOT / "m5_implementation_todo.md"
DEFERRED_CONTRACTS = REPOSITORY_ROOT / "docs" / "deferred_milestone_contracts.md"
AUDITED_DOCS = (
    *sorted(M5_ROOT.glob("*.md")),
    REPOSITORY_ROOT / "docs" / "backend_architecture_skeleton.md",
    REPOSITORY_ROOT / "docs" / "backend_refactor_milestones.md",
    DEFERRED_CONTRACTS,
    REPOSITORY_ROOT / "docs" / "frontend_integration_contracts.md",
)


def _documented_json() -> list[dict[str, object]]:
    return [
        json.loads(block)
        for block in re.findall(
            r"^```json\s*\n(.*?)^```\s*$",
            API_GUIDE.read_text(encoding="utf-8"),
            re.DOTALL | re.MULTILINE,
        )
    ]


def test_documented_json_examples_parse_and_typed_contents_validate() -> None:
    blocks = _documented_json()
    assert len(blocks) == 12
    for index, model in (
        (1, EvidenceToolCall),
        (2, EvidenceSourceRecords),
        (3, EvidenceFragment),
        (4, EvidenceFragment),
        (5, EvidenceFragment),
        (6, EvidenceToolResult),
        (7, EvidenceBundle),
    ):
        model.model_validate(blocks[index])


def test_m5_contract_matrix_covers_the_normative_contract_id_set() -> None:
    expected = {
        *(f"GLOBAL-C{index:02}" for index in range(1, 12)),
        *(f"M5-C{index:02}" for index in range(1, 21)),
        *(f"M5-A{index:02}" for index in range(1, 9)),
    }
    contracts = DEFERRED_CONTRACTS.read_text(encoding="utf-8")
    normative = set(
        re.findall(r"\*\*((?:GLOBAL|M5)-[CA]\d{2})\b", contracts)
    )
    assert expected <= normative

    todo = IMPLEMENTATION_TODO.read_text(encoding="utf-8")
    matrix = todo.split("## 契约覆盖矩阵", 1)[1].split(
        "## 实际完成记录格式", 1
    )[0]
    covered: set[str] = set()
    pattern = re.compile(
        r"(GLOBAL|M5)-([CA])(\d{2})"
        r"(?:–(?:(?:GLOBAL|M5)-)?(?:[CA])?(\d{2}))?"
    )
    for prefix, kind, start_text, end_text in pattern.findall(matrix):
        start = int(start_text)
        end = int(end_text or start_text)
        covered.update(
            f"{prefix}-{kind}{index:02}" for index in range(start, end + 1)
        )
    assert covered == expected


def test_docs_have_balanced_fences_valid_local_targets_and_no_trailing_space() -> None:
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


def test_runtime_registry_openapi_and_manifest_match_documented_fixed_counts() -> None:
    capabilities = load_parameter_capability_registry()
    assert len(capabilities.capabilities) == EXPECTED_PARAMETER_CAPABILITY_COUNT == 154
    assert len(create_evidence_tool_registry(capabilities).descriptors) == 8

    evidence_types = {
        "evidence_tool_call",
        "evidence_source_records",
        "evidence_fragment",
        "evidence_tool_result",
        "evidence_bundle",
    }
    registry = create_production_registry()
    assert evidence_types == {
        content_type
        for content_type, version in registry.writable_keys
        if content_type.startswith("evidence_") and version == 1
    }

    paths = set(create_test_app().openapi()["paths"])
    assert paths == {
        "/health",
        "/api/settings",
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
        for forbidden in ("/__test__", "/tools", "/evidence/collect")
    )
