import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

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


def main() -> None:
    port = available_loopback_port()
    process_environment = os.environ.copy()
    process_environment["PYTHONIOENCODING"] = "utf-8"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
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


if __name__ == "__main__":
    main()
