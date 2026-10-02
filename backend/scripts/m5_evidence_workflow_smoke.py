from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
SMOKE_TARGETS = (
    "tests/application/evidence_tools/test_acquisition_workflow.py",
    "tests/application/evidence_tools/test_dedup.py",
    "tests/application/evidence_tools/test_executor.py::test_unavailable_producer_budget_and_response_limits_return_safe_results",
    "tests/application/evidence_tools/test_bundle_builder.py::test_target_and_discharge_missing_states_are_independent",
    "tests/application/evidence_tools/test_bundle_builder.py::test_bundle_serialization_is_order_independent_and_write_failure_is_not_committed",
    "tests/api/test_m5_test_app.py",
)


def main() -> None:
    completed = subprocess.run(
        (
            sys.executable,
            "-B",
            "-m",
            "pytest",
            *SMOKE_TARGETS,
            "-q",
        ),
        cwd=BACKEND_ROOT,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)
    print(
        json.dumps(
            {
                "m5_evidence_workflow": "passed",
                "profiles": [
                    "station_backed_usable_partial_empty_failure",
                    "stationless_upstream_optional",
                    "per_window_discharge_gate",
                    "cancel_single_flight_budget_persistence_determinism",
                    "test_http_and_transcript_closure",
                ],
            },
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
