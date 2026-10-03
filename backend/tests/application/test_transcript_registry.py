import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.application.analysis_runs.models import (
    PublicError,
    RunCreatedEvent,
    SessionCreatedEvent,
)
from app.application.transcripts.models import (
    ContentVisibility,
    ParseStatus,
    StoredTranscriptItem,
)
from app.application.transcripts.registry import (
    UnsupportedTranscriptContentError,
    create_production_registry,
    extract_analysis_detail_references,
    validate_references,
)
from app.domain.attribution import AnalysisDetail, AnalysisStep
from scripts.transcript_fixtures import fixture_message_registration
from tests.m4_support import (
    PARSER_MODEL,
    WATERBODY_MODEL,
    create_request,
)

RUN_ID = UUID("10000000-0000-0000-0000-000000000001")
OTHER_RUN_ID = UUID("10000000-0000-0000-0000-000000000002")
SESSION_ID = UUID("20000000-0000-0000-0000-000000000001")
ITEM_ID = UUID("30000000-0000-0000-0000-000000000001")
OTHER_ITEM_ID = UUID("30000000-0000-0000-0000-000000000002")
ERROR_ID = UUID("40000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 9, 13, tzinfo=UTC)
CONFIGURATION = {
    "analysis_timezone": "Asia/Shanghai",
    "time_resolution_preference": "12h",
    "time_resolution_preference_source": "frontend",
    "parser_model": PARSER_MODEL.model_dump(mode="json"),
    "waterbody_model": WATERBODY_MODEL.model_dump(mode="json"),
    "memory_mode": "stateless",
}


def _stored(
    item_id: UUID,
    *,
    content_type: str = "analysis_request",
    version: int = 1,
    sequence: int = 1,
    body: str = "{}",
) -> StoredTranscriptItem:
    return StoredTranscriptItem(
        item_id=item_id,
        run_id=RUN_ID,
        transcript_sequence=sequence,
        created_at=NOW,
        content_type=content_type,
        content_schema_version=version,
        inline_body=body,
        content_bytes=len(body.encode("utf-8")),
    )


def test_production_registry_has_only_real_writable_types() -> None:
    registry = create_production_registry()

    assert registry.writable_keys == (
        ("analysis_request", 1),
        ("lifecycle_event", 2),
        ("error", 1),
        ("runtime_fact", 1),
        ("query_parser_input", 1),
        ("query_parser_message", 1),
        ("query_understood", 1),
        ("topology_ready", 1),
        ("evidence_tool_call", 1),
        ("evidence_source_records", 1),
        ("evidence_fragment", 1),
        ("evidence_tool_result", 1),
        ("evidence_bundle", 1),
        ("waterbody_agent_input", 1),
        ("waterbody_model_request", 1),
        ("waterbody_agent_message", 1),
        ("waterbody_model_response", 1),
        ("reasoning_decision", 1),
        ("inference_result", 1),
        ("waterbody_dependency_manifest", 1),
        ("run_summary_input", 1),
        ("run_summary_model_request", 1),
        ("run_summary_agent_message", 1),
        ("run_summary_model_response", 1),
        ("run_summary_result", 1),
        ("topology_run_result", 1),
    )
    assert ("fixture_message", 1) not in registry.registered_keys
    with pytest.raises(UnsupportedTranscriptContentError):
        registry.encode("fixture_message", 1, {"text": "not production"})


def test_analysis_request_round_trip_preserves_resolution_presence() -> None:
    registry = create_production_registry()
    omitted = registry.encode("analysis_request", 1, create_request(" 太湖水位 "))
    explicit = registry.encode(
        "analysis_request", 1, create_request("太湖水位", time_resolution="6h")
    )

    assert json.loads(omitted.safe_body) == create_request("太湖水位")
    assert json.loads(explicit.safe_body) == create_request(
        "太湖水位", time_resolution="6h"
    )
    parsed = registry.parse(_stored(ITEM_ID, body=omitted.safe_body), omitted.safe_body)
    assert parsed.parse_status is ParseStatus.OK
    assert parsed.body.model_fields_set == {
        "query", "parser_model", "waterbody_model", "memory_mode"
    }
    assert parsed.model_dump(mode="json", exclude_unset=True)["body"] == create_request(
        "太湖水位"
    )


@pytest.mark.parametrize(
    ("content_type", "version", "body"),
    [
        ("analysis_request", 2, {"query": "lake"}),
        ("analysis_request", 1, {"query": "lake", "extra": True}),
        ("analysis_request", 1, {"query": "lake", "time_resolution": None}),
        ("runtime_fact", 1, {"fact": "unknown"}),
        ("runtime_fact", 1, {"fact": "workflow_quiesced", "extra": True}),
        (
            "runtime_fact",
            1,
            {"fact": "suppressed_transcript_item", "reason": "completed"},
        ),
        (
            "runtime_fact",
            1,
            {
                "fact": "late_message_output",
                "original_item_id": str(ITEM_ID),
                "text": "",
                "end_reason": "cancelled",
            },
        ),
    ],
)
def test_new_writes_reject_unknown_versions_and_invalid_bodies(
    content_type: str,
    version: int,
    body: object,
) -> None:
    registry = create_production_registry()

    with pytest.raises((UnsupportedTranscriptContentError, ValidationError)):
        registry.encode(content_type, version, body)


def test_lifecycle_v2_keeps_run_and_session_names_and_single_configuration() -> None:
    registry = create_production_registry()
    run = RunCreatedEvent(
        run_id=RUN_ID,
        occurred_at=NOW,
        payload={"created_at": NOW, "configuration": CONFIGURATION},
    )
    session = SessionCreatedEvent(
        run_id=RUN_ID,
        session_id=SESSION_ID,
        occurred_at=NOW,
        payload={"created_at": NOW},
    )

    run_body = json.loads(registry.encode("lifecycle_event", 2, run).safe_body)
    session_body = json.loads(registry.encode("lifecycle_event", 2, session).safe_body)
    assert run_body["type"] == "run_created"
    assert run_body["payload"]["configuration"] == CONFIGURATION
    assert session_body["type"] == "session_created"
    assert "configuration" not in session_body["payload"]
    assert "agent_role" not in session_body["payload"]


def test_lifecycle_v2_accepts_uuid_ending_in_sensitive_key_prefix() -> None:
    run_id = UUID("4914ec8b-6d77-4bd2-bf65-e8aa93289acc")
    run = RunCreatedEvent(
        run_id=run_id,
        occurred_at=NOW,
        payload={"created_at": NOW, "configuration": CONFIGURATION},
    )

    encoded = create_production_registry().encode("lifecycle_event", 2, run)

    assert json.loads(encoded.safe_body)["run_id"] == str(run_id)


def test_legacy_lifecycle_v1_is_readable_but_never_rewritten() -> None:
    registry = create_production_registry()
    legacy = {
        "schema_version": 1,
        "session_id": str(SESSION_ID),
        "sequence": 1,
        "occurred_at": NOW.isoformat().replace("+00:00", "Z"),
        "type": "session_created",
        "payload": {
            "status": "pending",
            "created_at": NOW.isoformat().replace("+00:00", "Z"),
            "configuration": CONFIGURATION,
        },
    }

    legacy_body = json.dumps(legacy)
    parsed = registry.parse(
        _stored(
            ITEM_ID,
            content_type="lifecycle_event",
            version=1,
            body=legacy_body,
        ),
        legacy_body,
    )
    assert parsed.parse_status is ParseStatus.OK
    assert parsed.body.schema_version == 1
    assert parsed.body.session_id == SESSION_ID
    assert parsed.body.sequence == 1
    assert parsed.body.occurred_at == NOW
    assert not hasattr(parsed.body, "run_id")
    with pytest.raises(UnsupportedTranscriptContentError):
        registry.encode("lifecycle_event", 1, legacy)


def test_runtime_fact_union_has_exact_fields_references_and_visibility() -> None:
    registry = create_production_registry()
    normal = registry.encode("runtime_fact", 1, {"fact": "workflow_quiesced"})
    recovered = registry.encode(
        "runtime_fact", 1, {"fact": "persistence_recovered", "error_id": str(ERROR_ID)}
    )
    suppressed = registry.encode(
        "runtime_fact",
        1,
        {"fact": "suppressed_transcript_item", "reason": "cancelled"},
    )
    late = registry.encode(
        "runtime_fact",
        1,
        {
            "fact": "late_message_output",
            "original_item_id": str(ITEM_ID),
            "text": "late",
            "end_reason": "cancelled",
        },
    )

    assert (
        normal.references
        == recovered.references
        == suppressed.references
        == ()
    )
    assert late.references == (ITEM_ID,)
    assert suppressed.visibility is ContentVisibility.HISTORY_ONLY
    assert late.visibility is ContentVisibility.HISTORY_ONLY
    assert normal.metadata.tags == ()
    assert normal.metadata.rag_eligible is False
    assert normal.metadata.confidence is normal.metadata.provenance is None


def test_unknown_and_corrupt_history_are_distinct_and_keep_neighbors() -> None:
    registry = create_production_registry()
    good_body = registry.encode(
        "analysis_request", 1, create_request("lake")
    ).safe_body
    fixed_error = iter((UUID(int=1), UUID(int=2))).__next__
    results = registry.parse_many(
        (
            (_stored(ITEM_ID, body=good_body), good_body),
            (_stored(OTHER_ITEM_ID, version=99, sequence=2, body=good_body), good_body),
            (_stored(UUID(int=9), sequence=3, body='{"query":7}'), '{"query":7}'),
        ),
        error_id_factory=fixed_error,
    )

    assert [result.parse_status for result in results] == [
        ParseStatus.OK,
        ParseStatus.UNSUPPORTED,
        ParseStatus.CORRUPT,
    ]
    assert results[1].content_ref.item_id == OTHER_ITEM_ID
    assert results[1].parse_error.code == "transcript_content_unsupported"
    assert results[2].parse_error.code == "transcript_content_corrupt"
    assert results[0].body.query == "lake"


@pytest.mark.parametrize(
    "content_type",
    [
        "evidence_tool_call",
        "evidence_source_records",
        "evidence_fragment",
        "evidence_tool_result",
        "evidence_bundle",
    ],
)
def test_m5_content_reports_unknown_schema_and_corrupt_body(
    content_type: str,
) -> None:
    registry = create_production_registry()
    unknown = registry.parse(
        _stored(ITEM_ID, content_type=content_type, version=99),
        "{}",
        error_id_factory=lambda: UUID(int=1),
    )
    corrupt = registry.parse(
        _stored(OTHER_ITEM_ID, content_type=content_type),
        "{}",
        error_id_factory=lambda: UUID(int=2),
    )

    assert unknown.parse_status is ParseStatus.UNSUPPORTED
    assert unknown.parse_error.code == "transcript_content_unsupported"
    assert corrupt.parse_status is ParseStatus.CORRUPT
    assert corrupt.parse_error.code == "transcript_content_corrupt"


def test_read_detects_body_byte_count_corruption() -> None:
    registry = create_production_registry()
    body = registry.encode(
        "analysis_request", 1, create_request("lake")
    ).safe_body
    item = _stored(ITEM_ID, body=body).model_copy(update={
        "inline_body": None,
        "preview_head": "head",
        "preview_tail": "tail",
        "content_bytes": 1,
    })

    parsed = registry.parse(item, body, error_id_factory=lambda: UUID(int=1))

    assert parsed.parse_status is ParseStatus.CORRUPT
    assert parsed.parse_error.code == "transcript_content_corrupt"


def test_safe_read_body_redacts_valid_json_without_enforcing_known_schema() -> None:
    registry = create_production_registry()
    unsafe_body = json.dumps(
        {
            "query": 7,
            "nested": {
                "api_key": "nested-secret",
                "note": "Authorization: Basic dXNlcjpwYXNz",
            },
            "private_key": "private-secret",
        },
        ensure_ascii=False,
        indent=2,
    )

    safe_body = registry.safe_read_body("analysis_request", 1, unsafe_body)

    assert safe_body == (
        '{"query":7,"nested":{"api_key":"[REDACTED]",'
        '"note":"Authorization: [REDACTED]"},"private_key":"[REDACTED]"}'
    )
    assert "nested-secret" not in safe_body
    assert "dXNlcjpwYXNz" not in safe_body
    assert "private-secret" not in safe_body
    parsed = registry.parse(
        _stored(ITEM_ID, body=safe_body),
        safe_body,
        error_id_factory=lambda: UUID(int=1),
    )
    assert parsed.parse_status is ParseStatus.CORRUPT
    assert parsed.parse_error.code == "transcript_content_corrupt"


def test_standalone_error_is_strict_and_lifecycle_error_needs_no_copy() -> None:
    registry = create_production_registry()
    error = PublicError(
        error_id=ERROR_ID,
        code="workflow_failed",
        message="The workflow failed.",
        retryable=False,
    )

    encoded = registry.encode("error", 1, error)
    assert json.loads(encoded.safe_body) == error.model_dump(mode="json")
    assert encoded.references == ()
    with pytest.raises(ValidationError):
        registry.encode("error", 1, {**error.model_dump(), "configuration": CONFIGURATION})


def test_analysis_detail_references_are_preserved_and_uniquely_extracted() -> None:
    detail = AnalysisDetail(
        summary="done",
        transcript_item_refs=(str(ITEM_ID),),
        steps=(
            AnalysisStep(
                type="reasoning",
                title="step",
                content={"safe": True},
                transcript_item_refs=(str(ITEM_ID), str(OTHER_ITEM_ID)),
            ),
        ),
    )

    assert extract_analysis_detail_references(detail) == (ITEM_ID, OTHER_ITEM_ID)
    assert detail.transcript_item_refs == (str(ITEM_ID),)
    assert detail.steps[0].transcript_item_refs == (str(ITEM_ID), str(OTHER_ITEM_ID))


def test_reference_validation_allows_cross_session_but_not_cross_run() -> None:
    from app.application.transcripts.models import TranscriptContentRef

    same_run = TranscriptContentRef(run_id=RUN_ID, item_id=ITEM_ID)
    cross_run = TranscriptContentRef(run_id=OTHER_RUN_ID, item_id=ITEM_ID)

    assert validate_references(RUN_ID, (ITEM_ID,), persisted=(same_run,)).status.value == "ready"
    assert validate_references(RUN_ID, (ITEM_ID,), same_batch=(same_run,)).status.value == "ready"
    waiting = validate_references(RUN_ID, (ITEM_ID,), generating=(same_run,))
    assert waiting.status.value == "waiting"
    assert waiting.waiting_for == (same_run,)
    with pytest.raises(ValueError, match="same run"):
        validate_references(RUN_ID, (ITEM_ID,), persisted=(cross_run,))
    with pytest.raises(ValueError, match="does not exist"):
        validate_references(RUN_ID, (OTHER_ITEM_ID,))


def test_fixture_message_is_injected_only_for_generic_message_adapter_tests() -> None:
    registry = create_production_registry().copy_with(fixture_message_registration())
    encoded = registry.encode(
        "fixture_message",
        1,
        {"text": "safe delta", "generation_status": "completed", "end_reason": "completed"},
    )
    projection = registry.message_projection("fixture_message", 1, encoded.safe_body)

    assert projection.text == "safe delta"
    assert projection.generation_status.value == "completed"
    assert projection.end_reason.value == "completed"
