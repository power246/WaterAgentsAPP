from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
SMOKE_TARGETS = (
    "tests/smoke/test_m7_topology_workflow_smoke.py",
    "tests/application/analysis_runs/test_topology_driven_workflow.py",
    "tests/application/analysis_runs/test_topology_scheduler.py",
    "tests/application/analysis_runs/test_topology_scheduler_races.py",
    "tests/application/analysis_runs/test_node_launch_policy.py",
    "tests/application/analysis_runs/test_zero_model_result.py",
    "tests/application/analysis_runs/test_topology_cancellation.py",
    "tests/application/analysis_runs/test_topology_races.py",
    "tests/application/analysis_runs/test_workflow_quiescence.py",
    "tests/infrastructure/test_m7_runtime.py",
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
                "m7_topology_workflow": "passed",
                "profiles": [
                    "three_node_chain_and_diamond",
                    "stationless_all_insufficient",
                    "invalid_usable_and_partial",
                    "target_and_summary_failure",
                    "maximum_topology_and_same_model_summary",
                    "cancel_and_workflow_quiescence",
                ],
            },
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
