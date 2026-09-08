# WaterAgents backend

This directory contains the new WaterAgents backend. It requires Python 3.11 or newer and uses the same source and project configuration on macOS, Windows, and Linux.

## Create the local environment

The environment lives at `backend/.venv` and is intentionally ignored by Git. Virtual environments contain platform-specific executables, so create a fresh one on each machine instead of copying or committing it.

If `uv` is installed, the shortest path from the repository root creates or synchronizes `backend/.venv` from the committed lock file and starts the server:

```text
uv run --project backend --extra dev python -m app
```

The standard-library alternative requires no project-specific package manager.

macOS or Linux:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Windows PowerShell:

```powershell
Set-Location backend
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Python 3.12 or newer may be selected instead of 3.11. After activation, all platforms use the same commands below.

## Run

```text
python -m app
```

The defaults are `127.0.0.1:8000` and analysis timezone `America/Chicago`. Check the service from another terminal:

```text
python -c "import httpx; response = httpx.get('http://127.0.0.1:8000/health'); response.raise_for_status(); print(response.json())"
```

Expected response:

```json
{"status": "healthy"}
```

The equivalent explicit server command is:

```text
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

From the repository root, the cross-platform smoke command starts the formal `python -m app` entry point on an available loopback port, verifies the exact health response, and stops the child process:

```text
python backend/scripts/health_smoke.py
```

The session/SSE smoke runs the real application factory and Uvicorn on dynamic loopback sockets. It verifies completion, gated cancellation, keepalive, reconnection, terminal EOF, and a consumed cursor returning 204:

```text
python backend/scripts/session_sse_smoke.py
```

Expected stdout is `{"completed":"passed","cancelled":"passed"}`. The script controls startup/shutdown without a shell or process signals and cleans up the server before returning. Both smoke clients bypass environment proxies.

Use one Uvicorn worker. Sessions and events stay in memory and are lost on restart. See the [M2 API guide](../docs/M2/m2_analysis_session_sse_api.md) for the session protocol and limits.

The `Backend M1-M2` GitHub Actions workflow runs editable installation, the complete test suite, and both smoke checks with Python 3.11 on Ubuntu, macOS, and Windows. Smoke steps also run with unusable environment proxies to verify local connection isolation.

## Test

From `backend/`:

```text
python -m pytest -q
```

From the repository root:

```text
python -m pytest backend/tests -q
```

## Configuration

Settings use the `WATERAGENTS_` environment variable prefix:

| Variable | Default |
| --- | --- |
| `WATERAGENTS_APP_NAME` | `WaterAgents API` |
| `WATERAGENTS_ENVIRONMENT` | `development` |
| `WATERAGENTS_LOG_LEVEL` | `info` |
| `WATERAGENTS_HOST` | `127.0.0.1` |
| `WATERAGENTS_PORT` | `8000` |
| `WATERAGENTS_ANALYSIS_TIMEZONE` | `America/Chicago` |

Timezone names must be valid IANA identifiers. The project includes `tzdata` so this behaves consistently when the operating system does not provide an IANA timezone database.

At application startup, `wateragents.*` logs use single-line JSON on stderr at `WATERAGENTS_LOG_LEVEL`. They include request/session/error IDs, HTTP duration, committed lifecycle events, and redacted exception diagnostics. Uvicorn and third-party log formats are unchanged. See the [M2 API troubleshooting guide](../docs/M2/m2_analysis_session_sse_api.md#8-排障-runbook) for fields and event names.
