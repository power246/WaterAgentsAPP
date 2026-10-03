import ast
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from app.application.analysis_runs.models import (
    AgentRole,
    AgentSessionStatus,
    AnalysisPhase,
    RunSummarySessionResultSummary,
    SessionResultReference,
    SessionResultSummary,
    SessionSummary,
    WaterbodySessionResultSummary,
)
from app.application.transcripts.registry import create_production_registry

RUN_ID = UUID("861df3df-b7ad-4bb2-b4f2-34d35d2fa286")
SESSION_ID = UUID("961df3df-b7ad-4bb2-b4f2-34d35d2fa287")
ITEM_ID = UUID("a61df3df-b7ad-4bb2-b4f2-34d35d2fa288")
NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


def test_m7_extends_the_only_current_v1_role_and_phase_sets() -> None:
    assert tuple(role.value for role in AgentRole) == (
        "query_parser",
        "waterbody",
        "run_summarizer",
    )
    assert tuple(phase.value for phase in AnalysisPhase) == (
        "queued",
        "query_understanding",
        "topology_building",
        "topology_ready",
        "waterbody_execution",
        "run_summarization",
        "terminal",
    )


def test_current_waterbody_result_payload_without_discriminator_still_parses() -> None:
    parsed = TypeAdapter(SessionResultSummary).validate_python(
        {
            "result_id": "result:300",
            "target_comid": "300",
            "risk_level": "low",
            "availability": "partial",
            "overall_confidence": 0.7,
        }
    )

    assert isinstance(parsed, WaterbodySessionResultSummary)
    assert parsed.result_kind == "waterbody"


def test_session_summary_role_comid_and_result_matrix_is_strict() -> None:
    reference = SessionResultReference(run_id=RUN_ID, item_id=ITEM_ID)
    waterbody_result = WaterbodySessionResultSummary(
        result_id="result:300",
        target_comid="300",
        risk_level="low",
        availability="partial",
        overall_confidence=0.7,
    )
    summary_result = RunSummarySessionResultSummary(
        highlight_count=2,
        language="en",
    )
    valid = (
        SessionSummary(
            session_id=SESSION_ID,
            agent_role="query_parser",
            status="completed",
            created_at=NOW,
            updated_at=NOW,
        ),
        SessionSummary(
            session_id=SESSION_ID,
            agent_role="waterbody",
            target_comid="300",
            status="completed",
            created_at=NOW,
            updated_at=NOW,
            result_ref=reference,
            result_summary=waterbody_result,
        ),
        SessionSummary(
            session_id=SESSION_ID,
            agent_role="run_summarizer",
            status="completed",
            created_at=NOW,
            updated_at=NOW,
            result_ref=reference,
            result_summary=summary_result,
        ),
    )

    assert tuple(item.agent_role for item in valid) == tuple(AgentRole)
    invalid = (
        {"agent_role": "query_parser", "target_comid": "300"},
        {"agent_role": "waterbody", "target_comid": None},
        {"agent_role": "run_summarizer", "target_comid": "300"},
    )
    for change in invalid:
        with pytest.raises(ValidationError, match="only waterbody"):
            SessionSummary(
                session_id=SESSION_ID,
                status=AgentSessionStatus.PENDING,
                created_at=NOW,
                updated_at=NOW,
                **change,
            )
    with pytest.raises(ValidationError, match="waterbody result"):
        SessionSummary(
            session_id=SESSION_ID,
            agent_role="waterbody",
            target_comid="300",
            status="completed",
            created_at=NOW,
            updated_at=NOW,
            result_ref=reference,
            result_summary=summary_result,
        )
    with pytest.raises(ValidationError, match="run summary result"):
        SessionSummary(
            session_id=SESSION_ID,
            agent_role="run_summarizer",
            status="completed",
            created_at=NOW,
            updated_at=NOW,
            result_ref=reference,
            result_summary=waterbody_result,
        )


def test_m7_payload_modules_do_not_import_transport_provider_or_persistence() -> None:
    app_root = Path(__file__).resolve().parents[2] / "app"
    paths = (
        app_root / "application" / "analysis_runs" / "topology_result_models.py",
        app_root / "application" / "run_summary" / "models.py",
    )
    forbidden = (
        "app.api",
        "app.infrastructure",
        "app.ports",
        "fastapi",
        "httpx",
        "openai",
        "sqlalchemy",
    )

    imports: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
    assert not [
        name
        for name in imports
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in forbidden)
    ]


def test_m7_current_v1_types_are_registered_without_v2_forks() -> None:
    m7_content_types = {
        "waterbody_dependency_manifest",
        "run_summary_input",
        "run_summary_model_request",
        "run_summary_agent_message",
        "run_summary_model_response",
        "run_summary_result",
        "topology_run_result",
    }
    registered = {
        content_type
        for content_type, _version in create_production_registry().registered_keys
    }

    assert m7_content_types <= registered
    assert not any(content_type.endswith("_v2") for content_type in m7_content_types)
