from tests.m4_support import create_test_app


def test_m7_schemas_remain_with_m9_settings_route() -> None:
    schema = create_test_app().openapi()
    assert set(schema["paths"]) == {
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
        "/api/settings",
        "/api/keys",
        "/api/keys/{key_name}",
        "/api/keys/{key_name}/test",
    }
    schemas = schema["components"]["schemas"]
    assert {
        "result_ref",
        "summary",
        "graph_outcome",
        "waterbodies",
    } <= set(schemas["RunSnapshotEvent"]["properties"])
    assert {"result_ref", "result_summary"} <= set(
        schemas["RunSummary"]["properties"]
    )
    assert "TopologyRunResultSummary" in schemas
    assert "RunSummarySessionResultSummary" in schemas
