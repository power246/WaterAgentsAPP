from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
SMOKE_TARGETS = (
    "tests/smoke/test_m8_memory_rag_smoke.py",
    "tests/application/transcript_memory",
    "tests/application/transcripts/test_m8_history_registry.py",
    "tests/persistence/test_m8_production_lifespan.py",
    "tests/persistence/test_rag_generations.py",
    "tests/persistence/test_rag_search.py",
    "tests/persistence/test_rag_cleanup.py",
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
                "m8_memory_rag": "passed",
                "profiles": [
                    "production_entrypoint_and_deterministic_embedding",
                    "multi_run_multi_waterbody_query_to_query",
                    "m5_m6_history_sse_and_consumption_links",
                    "typed_failure_cancel_and_current_priority",
                    "generation_switch_restart_and_cleanup",
                ],
            },
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
