import asyncio

from scripts.m4_workflow_smoke import run_smoke


def test_fixed_m4_workflow_smoke() -> None:
    result = asyncio.run(run_smoke())

    assert result["frontend_target_date"]["persistence_reload"] == "passed"
    assert result["query_fallback_mixed_partial"]["mode"] == "mixed"
    assert result["no_source"]["coverage"] == "none"
    assert result["technical_failure"]["error_code"] == (
        "station_availability_failed"
    )
