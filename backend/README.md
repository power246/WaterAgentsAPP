# WaterAgents backend

This directory contains the new WaterAgents backend. It requires Python 3.11 or newer and uses the same source and project configuration on macOS, Windows, and Linux.

## Create the local environment

The environment lives at `backend/.venv` and is intentionally ignored by Git. Virtual environments contain platform-specific executables, so create a fresh one on each machine instead of copying or committing it.

If `uv` is installed, create or synchronize `backend/.venv` from the committed lock file from the repository root:

```text
uv sync --project backend --extra dev --locked
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

Configure PostgreSQL and run the explicit schema commands first; the following sections show both local Compose and existing-database setups. The formal entry point rejects a missing `WATERAGENTS_DB_PASSWORD`:

```text
uv run --project backend --locked python -m app
```

The defaults are `127.0.0.1:8000` and analysis timezone `America/Chicago`. Check the service from another terminal:

```text
python -c "import httpx; response = httpx.get('http://127.0.0.1:8000/health'); response.raise_for_status(); print(response.json())"
```

Expected response:

```json
{"status": "healthy"}
```

The lower-level server command below bypasses the formal entry point's configuration preflight and is intended for debugging or injected tests:

```text
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log --timeout-graceful-shutdown 2 --log-level info
```

From the repository root, the cross-platform smoke command starts the formal `python -m app` entry point on an available loopback port, verifies the exact health response, and stops the child process:

```text
python backend/scripts/health_smoke.py
```

The run/SSE smoke runs the real application factory and Uvicorn on dynamic loopback sockets. It verifies complete run snapshots, completion, gated cancellation, keepalive, reconnection, the final persisted snapshot, `stream_end`, and terminal EOF:

```text
python backend/scripts/run_sse_smoke.py
```

Expected stdout is `{"completed":"passed","cancelled":"passed"}`. The script controls startup/shutdown without a shell or process signals and cleans up the server before returning. Both smoke clients bypass environment proxies.

The `python -m app` entry point explicitly uses one Uvicorn worker and ignores `WEB_CONCURRENCY`. It disables Uvicorn's default access log and gives active HTTP/SSE connections 2 seconds to finish during shutdown before cancelling their requests and entering application cleanup. Workflow cleanup then uses its separate 1-second cooperative grace period. Preserve the worker, access log, shutdown timeout, and server log level options when invoking Uvicorn directly. Milestone 7 provides persisted transcript and two live SSE layers: run snapshots are rebuilt from current facts, while session process frames use an in-memory replay log and finalized transcript messages are persisted once. Restarted or released live streams redirect clients to saved history with HTTP 410. Startup repair, cancellation-boundary message finalization, late-output audit, and live-state grace cleanup remain later work. The staged status is tracked in the [M3 implementation checklist](../docs/M3/m3_implementation_todo.md).

The `Backend M1-M2` GitHub Actions workflow installs the same `backend/uv.lock`, starts PostgreSQL 17, runs the complete test suite, and executes both smoke checks with Python 3.11 on Ubuntu, macOS, and Windows. Smoke steps also run with unusable environment proxies to verify local connection isolation.

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

Settings use the `WATERAGENTS_` environment variable prefix. Explicit constructor values have highest priority, followed by process environment, the fixed UTF-8 `backend/.env`, and defaults. Loading does not depend on the current working directory. Database passwords use `SecretStr`, are not trimmed or printed, and are passed as a separate URL field so characters such as `@:/#%$` are encoded correctly.

| Variable | Default |
| --- | --- |
| `WATERAGENTS_APP_NAME` | `WaterAgents API` |
| `WATERAGENTS_ENVIRONMENT` | `development` |
| `WATERAGENTS_LOG_LEVEL` | `info` |
| `WATERAGENTS_HOST` | `127.0.0.1` |
| `WATERAGENTS_PORT` | `8000` |
| `WATERAGENTS_ANALYSIS_TIMEZONE` | `America/Chicago` |
| `WATERAGENTS_DB_HOST` | `127.0.0.1` |
| `WATERAGENTS_DB_PORT` | `5432` |
| `WATERAGENTS_DB_NAME` | `wateragents` |
| `WATERAGENTS_DB_USER` | `wateragents` |
| `WATERAGENTS_DB_PASSWORD` | required; no default |
| `WATERAGENTS_DB_SSLMODE` | `disable` |
| `WATERAGENTS_DB_SSLROOTCERT` | unset |

### Local PostgreSQL with Compose

Copy `backend/.env.example` to `backend/.env`, replace the sample password, and leave that file uncommitted. Then, from the repository root:

```text
docker compose --env-file backend/.env -f backend/compose.yaml up -d --wait
uv run --project backend --locked wateragents-db upgrade
uv run --project backend --locked wateragents-db check
uv run --project backend --locked python -m app
```

Compose publishes PostgreSQL only on loopback and stores data in the named `wateragents-postgres-data` volume. `wateragents-db upgrade` is safe to repeat. `wateragents-db check` only reads `alembic_version`; it never creates or repairs schema and fails clearly for missing, old, newer, unknown, or multiple revisions.

Stop containers while retaining the database:

```text
docker compose --env-file backend/.env -f backend/compose.yaml down
```

To intentionally delete all local database data, add `--volumes` to that command. This cannot be undone.

### Existing or remote PostgreSQL

Point the same separate variables at an empty or previously upgraded PostgreSQL 17 database. Do not place credentials in a single URL. For a provider requiring TLS, use for example:

```text
WATERAGENTS_DB_HOST=db.example.internal
WATERAGENTS_DB_PORT=5432
WATERAGENTS_DB_NAME=wateragents
WATERAGENTS_DB_USER=wateragents_app
WATERAGENTS_DB_PASSWORD=replace-me
WATERAGENTS_DB_SSLMODE=verify-full
WATERAGENTS_DB_SSLROOTCERT=/absolute/path/to/provider-ca.pem
```

Run `wateragents-db upgrade` as an explicit deployment step, followed by `wateragents-db check`, before starting the application. Never grant the application more database privileges than its deployment model requires. Do not run multiple application workers or replicas yet: live coordination and the persistence integration are still single-process until later M3 milestones.

When moving to another OS or machine, check out the source and committed `backend/uv.lock`, create a new environment with `uv sync --project backend --extra dev --locked`, recreate the uncommitted UTF-8 `.env`, start or select PostgreSQL 17, and run the two database commands. Never copy an old virtual environment or database data directory between platforms.

Timezone names must be valid IANA identifiers. The project includes `tzdata` so this behaves consistently when the operating system does not provide an IANA timezone database.

At application startup, `wateragents.*` logs use single-line JSON on stderr at `WATERAGENTS_LOG_LEVEL`. They include request/run/session/error IDs, HTTP duration, committed lifecycle events, and redacted exception diagnostics. HTTP access records use route templates; Uvicorn's default access log is disabled so URLs containing credentials or private queries are not logged. Uvicorn server/error and third-party logs retain their own formats. The [M2 API troubleshooting guide](../docs/M2/m2_analysis_session_sse_api.md#8-排障-runbook) remains the historical M2 reference; use the M3 checklist for the current implementation stage.

`WATERAGENTS_LOG_LEVEL=trace` enables application trace logs; the formal entry point uses `debug` for Uvicorn in that case. Other levels pass through unchanged. When invoking Uvicorn directly, use `debug`, `info`, `warning`, `error`, or `critical` for its server log level; Uvicorn trace logs can expose private ASGI scope data. Application trace remains independently available through `WATERAGENTS_LOG_LEVEL`.

If a response stream fails after headers are sent, the application logs the redacted exception and cause chain, then raises a fixed safe error with the request ID and suppresses the original exception chain for server logging. The connection still aborts and the subscription closes; look up `http_stream_failed` by request ID for the diagnostic details.
