import inspect
import json
import re
from pathlib import Path

from app.api.schemas.analysis_runs import (
    AnalysisRunErrorResponse,
    AnalysisRunResponse,
    CreateAnalysisRunRequest,
)
from app.api.sse.encoder import encode_event
from app.api.sse.response import _session_payload
from app.application.query_understanding import AnalysisContext
from app.application.query_understanding.parser_models import (
    QueryParserInputV1,
    QueryParserMessageV1,
)
from app.application.transcripts.read_models import AnalysisContextSummary
from app.application.transcripts.registry import create_production_registry
from app.application.transcripts.stream_models import RunSnapshotEvent
from app.domain import HydrologyGraph
from tests.m4_support import create_test_app


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
API_GUIDE = REPOSITORY_ROOT / "docs" / "M4" / "m4_query_understanding_topology_api.md"
IMPLEMENTATION_TODO = (
    REPOSITORY_ROOT / "docs" / "M4" / "m4_implementation_todo.md"
)
DEFERRED_CONTRACTS = REPOSITORY_ROOT / "docs" / "deferred_milestone_contracts.md"


def documented_json() -> list[dict[str, object]]:
    guide = API_GUIDE.read_text(encoding="utf-8")
    return [
        json.loads(block)
        for block in re.findall(r"```json\n(.*?)\n```", guide, re.DOTALL)
    ]


def one_block(predicate) -> dict[str, object]:
    matches = [block for block in documented_json() if predicate(block)]
    assert len(matches) == 1
    return matches[0]


def test_documented_json_fixtures_validate_with_production_models_and_registry() -> None:
    registry = create_production_registry()
    request = one_block(
        lambda block: "query" in block
        and "parser_model" in block
        and "target" in block
        and "schema_version" not in block
    )
    receipt = one_block(
        lambda block: "run_id" in block
        and "configuration" in block
        and "analysis_phase" not in block
    )
    validation_error = one_block(lambda block: "validation" in block)
    parser_input = one_block(lambda block: "prompt_template_version" in block)
    parser_message = one_block(
        lambda block: "generation_status" in block and "text" in block
    )
    context = one_block(
        lambda block: block.get("schema_version") == 1
        and "decision_instant" in block
    )
    topology = one_block(
        lambda block: block.get("schema_version") == 1 and "nodes" in block
    )
    context_summary = one_block(
        lambda block: "location" in block
        and "date_range" in block
        and "schema_version" not in block
    )
    snapshot = one_block(lambda block: "analysis_phase" in block)

    CreateAnalysisRunRequest.model_validate(request)
    AnalysisRunResponse.model_validate(receipt)
    AnalysisRunErrorResponse.model_validate(validation_error)
    QueryParserInputV1.model_validate(parser_input)
    QueryParserMessageV1.model_validate(parser_message)
    AnalysisContextSummary.model_validate(context_summary)
    RunSnapshotEvent.model_validate(snapshot)

    for content_type, model, body in (
        ("analysis_request", CreateAnalysisRunRequest, request),
        ("query_parser_input", QueryParserInputV1, parser_input),
        ("query_parser_message", QueryParserMessageV1, parser_message),
        ("query_understood", AnalysisContext, context),
        ("topology_ready", HydrologyGraph, topology),
    ):
        validated = model.model_validate(body)
        encoded = registry.encode(content_type, 1, validated)
        assert json.loads(encoded.safe_body) == body


def test_documented_openapi_and_sse_contract_are_runtime_derived() -> None:
    guide = API_GUIDE.read_text(encoding="utf-8")
    schema = create_test_app().openapi()
    create_schema = schema["components"]["schemas"]["CreateAnalysisRunRequest"]
    assert create_schema["additionalProperties"] is False
    assert set(create_schema["required"]) == {
        "query",
        "parser_model",
        "waterbody_model",
        "memory_mode",
    }
    assert set(create_schema["properties"]) == {
        "query",
        "target",
        "date_range",
        "time_resolution",
        "parser_model",
        "waterbody_model",
        "memory_mode",
    }
    assert schema["components"]["schemas"]["AnalysisContext"]["properties"][
        "params_used"
    ]["maxItems"] == 6
    run_summary_schema = schema["components"]["schemas"]["RunSummary"]
    assert "context_summary" in run_summary_schema["required"]
    assert {"result_ref", "result_summary"}.issubset(
        run_summary_schema["properties"]
    )
    assert {item.get("type") for item in run_summary_schema["properties"][
        "context_summary"
    ]["anyOf"]} == {None, "null"}
    run_snapshot_schema = schema["components"]["schemas"]["RunSnapshotEvent"]
    assert {
        "analysis_phase",
        "context_ref",
        "topology_ref",
        "result_ref",
        "summary",
        "graph_outcome",
    }.issubset(
        run_snapshot_schema["required"]
    )
    assert schema["components"]["schemas"]["AgentRole"]["enum"] == [
        "query_parser",
        "waterbody",
        "run_summarizer",
    ]
    run_stream_schema = schema["paths"][
        "/api/analysis/runs/{run_id}/events"
    ]["get"]["responses"]["200"]["content"]["text/event-stream"]["schema"]
    assert run_stream_schema["x-event-data-schema"] == {
        "$ref": "#/components/schemas/RunSnapshotEvent"
    }
    assert schema["x-m4-parameter-capability-baseline"] == {
        "count": 154,
        "required_support": [{"code_system": "usgs", "code": "00060"}],
        "max_target_parameters": 6,
    }
    error_section = guide.split("## 7. 运行中业务错误", 1)[1].split(
        "## 8. 前端接入顺序", 1
    )[0]
    for error in schema["x-m4-public-errors"]:
        assert (
            f"`{error['code']}` | `{error['failed_stage']}` | "
            f"{'true' if error['retryable'] else 'false'}"
        ) in error_section

    documented_snapshot = RunSnapshotEvent.model_validate(
        one_block(lambda block: "analysis_phase" in block)
    )
    frame = encode_event("run_snapshot", documented_snapshot).decode("utf-8")
    assert frame.startswith("event: run_snapshot\ndata: {")
    assert "\nid:" not in frame
    assert json.loads(frame.splitlines()[1].removeprefix("data: ")) == one_block(
        lambda block: "analysis_phase" in block
    )
    session_source = inspect.getsource(_session_payload)
    for event_name in (
        "transcript_item",
        "message_started",
        "message_delta",
        "message_finalized",
    ):
        assert f'"{event_name}"' in session_source
        assert f"event: {event_name}" in guide
    assert re.search(r"^event: topology_ready", guide, re.MULTILINE) is None


def test_m4_contract_matrix_covers_the_normative_contract_id_set() -> None:
    expected = {
        *(f"GLOBAL-C{index:02}" for index in range(1, 12)),
        *(f"M4-C{index:02}" for index in range(1, 14)),
        *(f"M4-A{index:02}" for index in range(1, 8)),
    }
    contracts = DEFERRED_CONTRACTS.read_text(encoding="utf-8")
    normative = set(
        re.findall(r"\*\*((?:GLOBAL|M4)-[CA]\d{2})\b", contracts)
    )
    assert expected <= normative

    todo = IMPLEMENTATION_TODO.read_text(encoding="utf-8")
    matrix = todo.split("## 契约覆盖矩阵", 1)[1].split(
        "## 实际完成记录格式", 1
    )[0]
    covered: set[str] = set()
    pattern = re.compile(
        r"(GLOBAL|M4)-([CA])(\d{2})"
        r"(?:–(?:(?:GLOBAL|M4)-)?(?:[CA])?(\d{2}))?"
    )
    for prefix, kind, start_text, end_text in pattern.findall(matrix):
        start = int(start_text)
        end = int(end_text or start_text)
        covered.update(
            f"{prefix}-{kind}{index:02}" for index in range(start, end + 1)
        )
    assert covered == expected
