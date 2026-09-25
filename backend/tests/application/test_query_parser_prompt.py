import json
from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from app.application.query_understanding import (
    ParserFinishReason,
    ParserMessageRole,
    ParserPromptMessage,
    QueryParserInputV1,
    QueryParserMessageV1,
    QueryParserPromptBuilder,
    QueryParserResponse,
)
from app.application.transcripts.models import (
    AnalysisDateRangeV1,
    AnalysisRequestV1,
    AnalysisTargetV1,
    GenerationStatus,
    MessageEndReason,
)
from app.application.transcripts.registry import create_production_registry
from app.domain import GeoPoint, MemoryMode, ModelSelection, TimeResolutionPreference
from app.infrastructure.config import load_parameter_capability_registry


PARSER_MODEL = ModelSelection(provider="provider-a", model="parser-v1")
WATERBODY_MODEL = ModelSelection(provider="provider-b", model="waterbody-v1")
SECRET = "Authorization: Bearer parser-secret"


def request(query: str = "Will dissolved oxygen be unsafe?") -> AnalysisRequestV1:
    return AnalysisRequestV1(
        query=query,
        target=AnalysisTargetV1(
            comid="12345678",
            coordinates=GeoPoint(latitude=38.9, longitude=-77.0),
        ),
        date_range=AnalysisDateRangeV1(
            start_date=date(2026, 9, 26),
            end_date=date(2026, 9, 28),
        ),
        time_resolution=TimeResolutionPreference.AUTO,
        parser_model=PARSER_MODEL,
        waterbody_model=WATERBODY_MODEL,
        memory_mode=MemoryMode.STATELESS,
    )


def test_prompt_contains_safe_request_context_schema_capabilities_and_few_shots() -> None:
    prompt = QueryParserPromptBuilder(
        load_parameter_capability_registry()
    ).build(
        request(SECRET),
        decision_instant=datetime(2026, 9, 25, 8, tzinfo=timezone.utc),
        analysis_timezone="America/New_York",
        private_values=("parser-secret",),
    )

    assert prompt.model == PARSER_MODEL
    assert prompt.tools == ()
    assert tuple(message.role for message in prompt.messages) == (
        ParserMessageRole.SYSTEM,
        ParserMessageRole.USER,
    )
    system = json.loads(prompt.messages[0].text)
    user = json.loads(prompt.messages[1].text)
    assert system["response_schema"]["title"] == "ParsedIntentV1"
    assert len(system["executable_capabilities"]) == 154
    assert len(system["few_shots"]) == 3
    assert user["structured_target"]["comid"] == "12345678"
    assert user["structured_date_range"] == {
        "start_date": "2026-09-26",
        "end_date": "2026-09-28",
    }
    assert user["resolution_preference"] == {
        "value": "auto",
        "source": "frontend",
    }
    assert user["decision"] == {
        "local_today": "2026-09-25",
        "analysis_timezone": "America/New_York",
    }
    assert SECRET not in prompt.model_dump_json()
    assert "parser-secret" not in prompt.model_dump_json()
    assert WATERBODY_MODEL.model not in prompt.model_dump_json()


def test_repair_request_contains_only_safe_output_and_validation_summary() -> None:
    builder = QueryParserPromptBuilder(load_parameter_capability_registry())
    initial = builder.build(
        request(),
        decision_instant=datetime(2026, 9, 25, 8, tzinfo=timezone.utc),
        analysis_timezone="America/New_York",
    )
    invalid_output = '{"parameters":[],"rationale":"api_key=parser-secret"}'
    with pytest.raises(ValidationError) as captured:
        from app.application.query_understanding import ParsedIntentV1

        ParsedIntentV1.model_validate_json(invalid_output)

    repair = builder.build_repair(
        initial,
        invalid_output,
        captured.value,
        private_values=("parser-secret",),
    )

    user = json.loads(repair.messages[1].text)
    assert set(user) == {"invalid_output", "validation_errors"}
    assert "parser-secret" not in repair.model_dump_json()
    assert "Will dissolved oxygen" not in repair.model_dump_json()
    assert "structured_target" not in repair.model_dump_json()
    assert repair.model == PARSER_MODEL
    assert repair.tools == ()


def test_parser_provider_contract_forbids_tools_and_unknown_response_fields() -> None:
    messages = (
        ParserPromptMessage(role="system", text="instructions"),
        ParserPromptMessage(role="user", text="request"),
    )

    with pytest.raises(ValidationError, match="tools must be empty"):
        QueryParserInputV1(model=PARSER_MODEL, messages=messages, tools=("geocode",))
    with pytest.raises(ValidationError):
        QueryParserResponse(
            text="{}",
            finish_reason=ParserFinishReason.STOP,
            tool_calls=("geocode",),
        )


def test_production_registry_round_trips_parser_input_and_message_projection() -> None:
    registry = create_production_registry()
    parser_input = QueryParserInputV1(
        model=PARSER_MODEL,
        messages=(
            ParserPromptMessage(role="system", text="instructions"),
            ParserPromptMessage(role="user", text="request"),
        ),
    )
    encoded_input = registry.encode("query_parser_input", 1, parser_input)
    assert json.loads(encoded_input.safe_body)["tools"] == []

    message = QueryParserMessageV1(
        text='{"parameters":[]}',
        generation_status=GenerationStatus.COMPLETED,
        end_reason=MessageEndReason.COMPLETED,
    )
    encoded_message = registry.encode("query_parser_message", 1, message)
    projection = registry.message_projection(
        "query_parser_message",
        1,
        encoded_message.safe_body,
    )
    assert projection.text == message.text
    assert projection.generation_status is GenerationStatus.COMPLETED
    assert projection.end_reason is MessageEndReason.COMPLETED
