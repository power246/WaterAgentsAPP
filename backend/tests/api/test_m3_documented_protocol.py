import inspect
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from app.api.http.errors import handle_error
from app.api.sse.encoder import encode_event
from app.application.analysis_runs.coordinator import RunCoordinator
from app.application.analysis_runs.models import (
    AnalysisConfigurationSnapshot,
    RunCreatedEvent,
    RunCreatedPayload,
)
from app.application.transcripts.registry import (
    TranscriptRegistry,
    create_production_registry,
)
from app.application.transcripts.stream_models import (
    MessageDeltaPayload,
    SessionProcessEvent,
    StreamEndEvent,
)
from app.domain.time.time_resolution import TimeResolution
from app.domain.time.time_resolution_source import TimeResolutionSource
from app.main import create_app

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
API_GUIDE = REPOSITORY_ROOT / "docs" / "M3" / "m3_transcript_history_api.md"
FRONTEND_GUIDE = REPOSITORY_ROOT / "docs" / "frontend_integration_contracts.md"
IMPLEMENTATION_CHECKLIST = (
    REPOSITORY_ROOT / "docs" / "M3" / "m3_implementation_todo.md"
)
CENTRAL_CONTRACTS = REPOSITORY_ROOT / "docs" / "deferred_milestone_contracts.md"
ARCHITECTURE_GUIDE = REPOSITORY_ROOT / "docs" / "backend_architecture_skeleton.md"
MILESTONE_GUIDE = REPOSITORY_ROOT / "docs" / "backend_refactor_milestones.md"
RUN_ID = UUID("10ef14bd-d0e7-4a70-9728-1fbb4ca7a83c")
ITEM_ID = UUID("c5677c98-27ac-4881-b7ba-b06cc2e85482")
SESSION_ID = UUID("7d0a9cea-d754-4d88-8271-f4a38b8fa71f")
MESSAGE_ID = UUID("6ad82df9-8d67-46ec-9599-2a8d3528d281")
REQUEST_ID = UUID("ec011edb-ef06-49f6-abd7-f2d9b587fd76")


def section(document: str, start: str, end: str) -> str:
    return document.split(start, 1)[1].split(end, 1)[0]


def test_documented_error_codes_match_runtime_names_and_openapi_statuses() -> None:
    guide = API_GUIDE.read_text(encoding="utf-8")
    error_section = section(guide, "## 7. 错误与排查字段", "## 8. 跨平台搭建与配置")
    schema = create_app().openapi()
    openapi_statuses = {
        int(status)
        for path, operations in schema["paths"].items()
        if path.startswith("/api/analysis/runs")
        for operation in operations.values()
        for status in operation["responses"]
        if int(status) >= 400
    }
    documented_statuses = {
        int(match.group(1))
        for match in re.finditer(r"^\| (\d{3}) \|", error_section, re.MULTILINE)
    }
    http_codes = {
        "invalid_request",
        "invalid_event_cursor",
        "run_not_found",
        "session_not_found",
        "transcript_item_not_found",
        "method_not_allowed",
        "idempotency_key_conflict",
        "event_cursor_ahead",
        "run_already_terminal",
        "persistence_retry_unavailable",
        "run_not_live",
        "live_state_gone",
        "validation_error",
        "invalid_history_cursor",
        "transcript_content_unreadable",
        "internal_error",
        "database_unavailable",
        "idempotency_wait_timeout",
        "creation_commit_unconfirmed",
        "service_unavailable",
    }
    handler_source = inspect.getsource(handle_error)

    assert documented_statuses == openapi_statuses
    assert all(f'"{code}"' in handler_source for code in http_codes)
    assert all(code in error_section for code in http_codes)
    assert not {
        "persistence_write_timeout",
        "unsupported_content",
        "persistence_identity_conflict",
    }.intersection(error_section)

    registry_source = inspect.getsource(TranscriptRegistry.parse)
    warning_source = inspect.getsource(RunCoordinator._persistence_error)
    for code in ("transcript_content_unsupported", "transcript_content_corrupt"):
        assert code in registry_source
        assert code in error_section
    for code in ("persistence_write_failed", "persistence_write_conflict"):
        assert code in warning_source
        assert code in error_section


def test_documented_run_and_session_statuses_match_openapi() -> None:
    schema = create_app().openapi()["components"]["schemas"]
    assert schema["RunStatus"]["enum"] == [
        "pending",
        "running",
        "completed",
        "failed",
        "cancelled",
        "completed_with_persistence_error",
    ]
    assert schema["AgentSessionStatus"]["enum"] == [
        "pending",
        "running",
        "completed",
        "failed",
        "cancelled",
    ]

    guide = FRONTEND_GUIDE.read_text(encoding="utf-8")
    persistence_section = section(
        guide,
        "### 4.5 持久化异常与取消后的内容",
        "### 4.6 重启或内存清理后的 SSE",
    )
    assert "只有 run 可以进入 `completed_with_persistence_error`" in persistence_section
    assert "session 始终只使用五态" in persistence_section
    assert "唯一 `run_completed`" in persistence_section
    assert "session terminal event 不携带 run 级保存 warning" in persistence_section


def test_documented_item_and_sse_frames_are_production_serialization_goldens() -> None:
    occurred_at = datetime(2026, 9, 13, 3, 20, tzinfo=UTC)
    event = RunCreatedEvent(
        run_id=RUN_ID,
        sequence=1,
        occurred_at=occurred_at,
        payload=RunCreatedPayload(
            created_at=occurred_at,
            configuration=AnalysisConfigurationSnapshot(
                analysis_timezone="America/Chicago",
                time_resolution=TimeResolution.TWELVE_HOURS,
                time_resolution_source=TimeResolutionSource.FRONTEND,
            ),
        ),
    )
    encoded = create_production_registry().encode("lifecycle_event", 2, event)
    guide = API_GUIDE.read_text(encoding="utf-8")
    item_section = section(guide, "### 3.4 完整 item 示例", "## 4. GET 读取接口")
    example = json.loads(item_section.split("```json", 1)[1].split("```", 1)[0])

    assert encoded.content_bytes == len(encoded.safe_body.encode("utf-8")) == 340
    assert example["item_id"] == str(ITEM_ID)
    assert example["run_id"] == str(RUN_ID)
    assert example["correlation"]["request_id"] == str(REQUEST_ID)
    assert example["content_bytes"] == encoded.content_bytes
    assert example["occurred_at"] == event.model_dump(mode="json")["occurred_at"]
    assert example["content"]["body"] == json.loads(encoded.safe_body)

    process_event = SessionProcessEvent(
        run_id=RUN_ID,
        session_id=SESSION_ID,
        sequence=3,
        occurred_at=datetime(2026, 9, 13, 3, 20, 0, 200000, tzinfo=UTC),
        payload=MessageDeltaPayload(
            item_id=MESSAGE_ID,
            text="已取得当前证据。",
        ),
    )
    delta_frame = encode_event("message_delta", process_event, event_id=3).decode()
    end_frame = encode_event(
        "stream_end",
        StreamEndEvent(run_id=RUN_ID, session_id=SESSION_ID, stream="session"),
    ).decode()
    assert delta_frame in guide
    assert end_frame in guide


def test_m3_contract_audit_covers_every_id_without_claiming_platform_evidence() -> None:
    checklist = IMPLEMENTATION_CHECKLIST.read_text(encoding="utf-8")
    central = CENTRAL_CONTRACTS.read_text(encoding="utf-8")
    matrix = section(checklist, "### 契约覆盖矩阵", "### GLOBAL / MR 门禁审计")
    milestone_19 = checklist.split(
        "## Milestone 19：追加修复集成验收与契约复审",
        1,
    )[1]
    central_m3 = section(central, "## 5. M3", "## 6. M4")
    expected_contracts = {
        *(f"M3-C{index:02d}" for index in range(1, 21)),
        *(f"M3-A{index:02d}" for index in range(1, 13)),
    }

    for source in (matrix, central_m3):
        assert set(re.findall(r"M3-[CA]\d{2}", source)) == expected_contracts
    assert "回补待验证" not in matrix
    for label in (
        "GLOBAL-C01–C03",
        "GLOBAL-C04、C09",
        "GLOBAL-C05–C06",
        "GLOBAL-C07",
        "GLOBAL-C08",
        "GLOBAL-C10",
        "GLOBAL-C11",
        "MR-G01–G02",
        "MR-G03–G10",
        "MR-G11–G12",
        "MR-G13–G14",
    ):
        assert label in checklist
    assert "GLOBAL-C11 | partial" in checklist
    assert "- [ ]" not in milestone_19
    assert "### Milestone 19 完成记录（2026-09-17）" in milestone_19
    assert "851 passed" in milestone_19
    assert "47 passed" in milestone_19
    assert "三平台实际结果" in central
    assert "保持未最终承接" in central


def test_parent_docs_describe_the_implemented_m3_public_protocol() -> None:
    schema = create_app().openapi()
    actual_routes = {
        path: {method for method in operations if method in {"get", "post"}}
        for path, operations in schema["paths"].items()
        if path.startswith("/api/analysis/runs")
    }
    expected_routes = {
        "/api/analysis/runs": {"get", "post"},
        "/api/analysis/runs/{run_id}": {"get"},
        "/api/analysis/runs/{run_id}/sessions/{session_id}": {"get"},
        "/api/analysis/runs/{run_id}/sessions/{session_id}/transcript": {"get"},
        "/api/analysis/runs/{run_id}/transcript/{item_id}/content": {"get"},
        "/api/analysis/runs/{run_id}/events": {"get"},
        "/api/analysis/runs/{run_id}/sessions/{session_id}/events": {"get"},
        "/api/analysis/runs/{run_id}/cancel": {"post"},
        "/api/analysis/runs/{run_id}/persistence/retry": {"post"},
    }
    assert actual_routes == expected_routes

    documents = {
        "architecture": ARCHITECTURE_GUIDE.read_text(encoding="utf-8"),
        "milestones": MILESTONE_GUIDE.read_text(encoding="utf-8"),
    }
    stale_m3_claims = (
        "当前 M2 以旧 session 命名实现整次分析及 SSE",
        "本次是文档交付，后端尚未实现",
        "当前代码尚未迁移",
        "本阶段尚未实现",
        "本次没有修改代码",
    )
    for document in documents.values():
        assert not any(claim in document for claim in stale_m3_claims)
        for path in expected_routes:
            assert path in document
        for event_name in ("message_started", "message_delta", "message_finalized"):
            assert event_name in document
        assert "M3 已在 `backend/` 实现" in document
        assert "Milestone 19 已完成" in document
        assert "`request changes` 已清除" in document
        assert "GLOBAL-C11" in document
        assert "partial" in document
