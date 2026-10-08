import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from app.infrastructure.runtime.event_loop import configure_asyncio_event_loop
from test_database import temporary_migrated_schema

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def available_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run_health_smoke(settings) -> None:
    port = available_loopback_port()
    process_environment = os.environ.copy()
    process_environment["PYTHONIOENCODING"] = "utf-8"
    process_environment["WATERAGENTS_HOST"] = "127.0.0.1"
    process_environment["WATERAGENTS_PORT"] = str(port)
    process_environment["WATERAGENTS_LOG_LEVEL"] = "INFO"
    process_environment["WATERAGENTS_DB_NAME"] = settings.db_name
    process_environment["WATERAGENTS_MODEL_CATALOG"] = json.dumps(
        [
            {
                "provider": "openai",
                "model": "health-smoke-parser",
                "roles": ["query_parser"],
            },
            {
                "provider": "openai",
                "model": "health-smoke-waterbody",
                "roles": ["waterbody"],
                "context_window_tokens": 128_000,
                "max_output_tokens": 32_768,
            },
            {
                "provider": "openai",
                "model": "health-smoke-embedding",
                "roles": ["embedding"],
                "embedding_profile": {
                    "provider": "openai",
                    "model": "health-smoke-embedding",
                    "output_dimensions": 8,
                    "max_input_tokens": 512,
                    "distance_metric": "cosine",
                    "semantic_min_similarity": 0.78,
                    "canonical_query_schema_version": 1,
                    "canonical_template_version": 1,
                    "projection_schema_version": 1,
                },
            },
        ],
        separators=(",", ":"),
    )
    process_environment["WATERAGENTS_OPENAI_API_KEY"] = "health-smoke-not-sent"
    process_environment["WATERAGENTS_GOOGLE_GEOCODING_API_KEY"] = (
        "health-smoke-not-sent"
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "app",
        ],
        cwd=BACKEND_ROOT,
        env=process_environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
    )
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + 15
    try:
        with httpx.Client(timeout=1, trust_env=False) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    output = process.communicate()[0]
                    raise RuntimeError(f"Uvicorn exited before health check:\n{output}")
                try:
                    response = client.get(url)
                except httpx.HTTPError:
                    time.sleep(0.1)
                    continue
                response.raise_for_status()
                payload = response.json()
                if payload != {"status": "healthy"}:
                    raise RuntimeError(f"unexpected health response: {payload!r}")
                print(json.dumps(payload, separators=(",", ":")))
                return
        raise TimeoutError("Uvicorn did not become healthy within 15 seconds")
    finally:
        stop_process(process)


def main() -> None:
    with temporary_migrated_schema() as settings:
        run_health_smoke(settings)


if __name__ == "__main__":
    configure_asyncio_event_loop()
    main()
