import json
from datetime import datetime
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from app.application.analysis_runs.models import (
    AgentRole,
    AgentSessionStatus,
    LifecycleEvent,
    PublicError,
    RunCompletedPayload,
    RunStatus,
    SessionCompletedEvent,
    SessionCreatedEvent,
    SessionLifecycleEvent,
    SessionSummary,
    TranscriptItemIdentity,
)

RUN_ID = UUID("10ef14bd-d0e7-4a70-9728-1fbb4ca7a83c")
SESSION_ID = UUID("3e3df1c6-9f51-4fae-a12a-2c78b3cb2cc2")
ITEM_ID = UUID("005f9320-4c95-41e7-b808-b02b48affb42")
ERROR = PublicError(
    error_id=UUID("0463cd90-6d0f-47e9-9656-b4aa86352c61"),
    code="persistence_failed",
    message="The analysis could not be fully saved.",
    retryable=True,
)
LOCAL_TIME = "2026-09-08T08:20:00-05:00"
UTC_TIME = "2026-09-08T13:20:00Z"
SESSION_EVENTS = TypeAdapter(SessionLifecycleEvent)
ALL_EVENTS = TypeAdapter(LifecycleEvent)


def session_data(**changes) -> dict:
    return {
        "session_id": str(SESSION_ID),
        "agent_role": "waterbody",
        "target_comid": "001234",
        "status": "pending",
        "created_at": LOCAL_TIME,
        "updated_at": LOCAL_TIME,
        **changes,
    }


def test_run_and_agent_session_status_sets_are_distinct() -> None:
    assert [status.value for status in AgentSessionStatus] == [
        "pending",
        "running",
        "completed",
        "failed",
        "cancelled",
    ]
    assert RunStatus.COMPLETED_WITH_PERSISTENCE_ERROR.value not in {
        status.value for status in AgentSessionStatus
    }
    assert [role.value for role in AgentRole] == ["query_parser", "waterbody"]


@pytest.mark.parametrize(
    ("role", "comid"),
    [
        ("query_parser", "001234"),
        ("waterbody", None),
        ("waterbody", ""),
        ("waterbody", "  "),
    ],
)
def test_session_summary_enforces_role_comid_pair(role, comid) -> None:
    with pytest.raises(ValidationError):
        SessionSummary.model_validate(
            session_data(agent_role=role, target_comid=comid)
        )


def test_query_parser_and_waterbody_summaries_round_trip_immutably() -> None:
    parser = SessionSummary.model_validate(
        session_data(agent_role="query_parser", target_comid=None)
    )
    waterbody = SessionSummary.model_validate(
        session_data(target_comid=" 001234 ")
    )

    assert parser.target_comid is None
    assert waterbody.target_comid == "001234"
    assert waterbody.model_dump(mode="json") == {
        **session_data(),
        "created_at": UTC_TIME,
        "updated_at": UTC_TIME,
    }
    assert SessionSummary.model_validate_json(waterbody.model_dump_json()) == waterbody
    with pytest.raises(ValidationError, match="frozen"):
        waterbody.status = AgentSessionStatus.COMPLETED


def test_session_summary_rejects_naive_or_reversed_timestamps() -> None:
    for field in ("created_at", "updated_at"):
        with pytest.raises(ValidationError, match="timezone"):
            SessionSummary.model_validate(
                session_data(**{field: datetime(2026, 9, 8)})
            )
    with pytest.raises(ValidationError, match="must not precede"):
        SessionSummary.model_validate(
            session_data(
                created_at="2026-09-08T13:20:01Z",
                updated_at="2026-09-08T13:20:00Z",
            )
        )


def test_session_lifecycle_v2_requires_run_and_session_scope() -> None:
    event = SessionCreatedEvent(
        run_id=RUN_ID,
        session_id=SESSION_ID,
        occurred_at=LOCAL_TIME,
        payload={"created_at": LOCAL_TIME},
    )

    assert event.model_dump(mode="json") == {
        "schema_version": 2,
        "run_id": str(RUN_ID),
        "session_id": str(SESSION_ID),
        "sequence": 1,
        "type": "session_created",
        "occurred_at": UTC_TIME,
        "payload": {"status": "pending", "created_at": UTC_TIME},
    }
    assert SESSION_EVENTS.validate_json(event.model_dump_json()) == event
    assert ALL_EVENTS.validate_python(event.model_dump()) == event
    with pytest.raises(ValidationError):
        SessionCreatedEvent.model_validate(
            {
                **event.model_dump(),
                "payload": {
                    **event.payload.model_dump(),
                    "configuration": {},
                },
            }
        )


def test_session_event_type_and_payload_are_strictly_paired() -> None:
    completed = SessionCompletedEvent(
        run_id=RUN_ID,
        session_id=SESSION_ID,
        sequence=2,
        occurred_at=LOCAL_TIME,
        payload={"completed_at": LOCAL_TIME},
    )
    wrong = {
        **completed.model_dump(mode="json"),
        "type": "session_cancelled",
    }

    with pytest.raises(ValidationError):
        SESSION_EVENTS.validate_json(json.dumps(wrong))
    with pytest.raises(ValidationError):
        SessionCompletedEvent.model_validate(
            {**completed.model_dump(), "session_id": None}
        )


def test_run_persistence_completion_requires_exact_warning_shape() -> None:
    normal = RunCompletedPayload(
        status=RunStatus.COMPLETED,
        completed_at=LOCAL_TIME,
    )
    degraded = RunCompletedPayload(
        status=RunStatus.COMPLETED_WITH_PERSISTENCE_ERROR,
        completed_at=LOCAL_TIME,
        warning=ERROR,
    )

    assert normal.warning is None
    assert degraded.warning is ERROR
    with pytest.raises(ValidationError, match="warning must match"):
        RunCompletedPayload(
            status=RunStatus.COMPLETED,
            completed_at=LOCAL_TIME,
            warning=ERROR,
        )
    with pytest.raises(ValidationError, match="warning must match"):
        RunCompletedPayload(
            status=RunStatus.COMPLETED_WITH_PERSISTENCE_ERROR,
            completed_at=LOCAL_TIME,
        )


def test_transcript_identity_is_immutable_strict_and_utc() -> None:
    identity = TranscriptItemIdentity(
        item_id=ITEM_ID,
        run_id=RUN_ID,
        session_id=SESSION_ID,
        transcript_sequence=4,
        created_at=LOCAL_TIME,
    )

    assert identity.created_at.isoformat() == "2026-09-08T13:20:00+00:00"
    assert TranscriptItemIdentity.model_validate_json(identity.model_dump_json()) == identity
    with pytest.raises(ValidationError):
        TranscriptItemIdentity(
            item_id=ITEM_ID,
            run_id=RUN_ID,
            transcript_sequence=0,
            created_at=LOCAL_TIME,
        )
    with pytest.raises(ValidationError, match="frozen"):
        identity.transcript_sequence = 5
