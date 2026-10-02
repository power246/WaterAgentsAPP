from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
SMOKE_TARGETS = (
    "tests/smoke/test_m6_reasoning_workflow_smoke.py",
    "tests/application/reasoning/test_runner.py",
    "tests/application/reasoning/test_m9_transcript_store.py",
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
                "m6_reasoning_workflow": "passed",
                "profiles": [
                    "single_waterbody_full_empty_and_failure",
                    "production_runner_writer_reader",
                    "typed_transcript_and_atomic_result",
                    "session_sse_replay_reconnect_and_safe_run_projection",
                ],
            },
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
