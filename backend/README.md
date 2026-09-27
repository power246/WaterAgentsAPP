# WaterAgents backend

This directory contains the new WaterAgents backend. It requires Python 3.11 or newer and uses the same source and project configuration on macOS, Windows, and Linux.

## Create the local environment

The environment lives at `backend/.venv` and is intentionally ignored by Git. Virtual environments contain platform-specific executables, so create a fresh one on each machine instead of copying or committing it.

If `uv` is installed, create or synchronize `backend/.venv` from the committed lock file from the repository root:

```text
uv sync --project backend --extra dev --locked
```

Then enter `backend/` and activate the environment.

macOS or Linux:

```bash
cd backend
source .venv/bin/activate
```

Windows PowerShell:

```powershell
Set-Location backend
.\.venv\Scripts\Activate.ps1
```

The standard-library alternative below does not require `uv`.

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

Python 3.12 or newer may be selected instead of 3.11. After activation, keep the current directory at `backend/`; every remaining command in this README is written for that directory and can be copied unchanged on all platforms unless a platform-specific block is shown.

## Run

Configure PostgreSQL and run the explicit schema commands first; the following sections show both local Compose and existing-database setups. The formal entry point rejects a missing `WATERAGENTS_DB_PASSWORD`:

```text
python -m app
```

The defaults are `127.0.0.1:8000` and analysis timezone `America/Chicago`. Check the service from another terminal:

```text
python -c "import httpx; response = httpx.get('http://127.0.0.1:8000/health', trust_env=False); response.raise_for_status(); print(response.json())"
```

Expected response:

```json
{"status": "healthy"}
```

The lower-level server command below still runs lifespan database checks, but bypasses the formal entry point's event-loop and worker wrapper. It is intended for macOS/Linux debugging; use the formal entry point in deployments. On Windows, use `python -m app`, or add `--loop app.infrastructure.runtime.event_loop:windows_selector_loop_factory` when diagnosing with Uvicorn directly:

```text
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log --timeout-graceful-shutdown 2 --log-level info
```

Create a dedicated empty test database, set `WATERAGENTS_TEST_DB_NAME` in the test process environment, and keep it different from `WATERAGENTS_DB_NAME`. Do not put this test-only variable in `backend/.env`; that file accepts formal runtime settings only. Both smoke scripts create and migrate a random temporary schema inside that test database, then delete only that schema. The health smoke starts the formal `python -m app` entry point on an available loopback port, verifies the exact response, and stops the child process:

```text
python scripts/health_smoke.py
```

The run/history smoke uses real Uvicorn and PostgreSQL on dynamic loopback sockets. It verifies both SSE layers, a large UTF-8 message and full-text read, completion, gated cancellation, persisted history, pagination, process restart repair, HTTP 410 history handoff, and the original idempotent creation receipt without a real LLM:

```text
python scripts/run_history_smoke.py
```

Expected final stdout is `{"history":"passed","independent_process_restart":"passed","restart_repair":"passed","database":"<test DB>"}`. The history smoke fully stops its first Uvicorn child process before starting a freshly imported application in a second child process against the same database. Both smoke scripts use cross-platform process APIs with terminate, bounded wait, and kill fallback; they do not invoke a shell or GNU utility. They bypass environment proxies and fail clearly if the test database variable or connection is missing.

The standalone run/SSE and M4 workflow smokes are deterministic and do not require PostgreSQL or external credentials. The M4 smoke composes the production parser/resolver/availability/topology workflow with fixed model, geocoding, NLDI, WDFN, and Samples fixtures:

```text
python scripts/run_sse_smoke.py
python scripts/m4_workflow_smoke.py
```

Audit the checked-in 154-item capability manifest offline with `python scripts/audit_parameter_capabilities.py`. The optional `--refresh-official` flag performs a bounded read of the official USGS Water Data OGC API v1 parameter catalog and reports drift; it never modifies or enables manifest entries.

The `python -m app` entry point explicitly uses one Uvicorn worker and ignores `WEB_CONCURRENCY`. On Windows it passes Uvicorn an explicit Selector loop factory compatible with psycopg async connections; macOS and Linux retain Uvicorn's `auto` selection. The run/history smoke gives its independent Uvicorn children the same platform-specific loop choice. Direct `asyncio.run()` callers keep the existing pre-start policy initialization. The entry point disables Uvicorn's default access log and gives active HTTP/SSE connections 2 seconds to finish during shutdown before entering application cleanup. Lifespan connects to PostgreSQL, requires the exact Alembic head, repairs saved non-terminal run/session state as `failed/service_interrupted`, and only then accepts traffic; it never runs DDL or resumes Agents automatically. User cancellation freezes active message prefixes and stores any later safe tail as history-only audit. Terminal live state is released only after workflow quiescence, required persistence, and the configured full grace period; saved history remains readable and SSE returns HTTP 410 afterward.

The `Backend M1-M9` GitHub Actions workflow installs the same `backend/uv.lock`, provisions PostgreSQL 17, runs the complete test suite, the offline M4 audit, and all four smoke checks with Python 3.11 on Ubuntu, macOS, and Windows. Smoke steps also run with unusable environment proxies to verify local connection isolation. The matrix is the reproducible CI entry; platform support is recorded as partial until each remote job has an actual passing result.

## Test

PostgreSQL integration tests are mandatory. Put only the normal `WATERAGENTS_DB_*` host, port, business database name, user, password, and SSL settings in `backend/.env`. Then set `WATERAGENTS_TEST_DB_NAME` to the pre-created test database in the shell that launches tests and smoke checks. Tests use temporary schemas and refuse a missing, empty, or business-identical test database name.

PowerShell:

```powershell
$env:WATERAGENTS_TEST_DB_NAME = 'wateragents_test'
python -m pytest -q
python scripts/health_smoke.py
python scripts/run_history_smoke.py
Remove-Item Env:WATERAGENTS_TEST_DB_NAME
```

bash:

```bash
export WATERAGENTS_TEST_DB_NAME='wateragents_test'
python -m pytest -q
python scripts/health_smoke.py
python scripts/run_history_smoke.py
unset WATERAGENTS_TEST_DB_NAME
```

## Configuration

Settings use the `WATERAGENTS_` environment variable prefix. Explicit constructor values have highest priority, followed by process environment, the fixed UTF-8 `backend/.env`, and defaults. Loading does not depend on the current working directory. `backend/.env` is production-runtime configuration: unknown or misspelled fields fail validation instead of being ignored. Database passwords and provider API keys use `SecretStr` and are not trimmed or printed. Provider adapters share the production `httpx` runtime, ignore environment proxies, bound response size and pagination, and never include credentials or full provider URLs in application logs.

| Variable | Default |
| --- | --- |
| `WATERAGENTS_APP_NAME` | `WaterAgents API` |
| `WATERAGENTS_ENVIRONMENT` | `development` |
| `WATERAGENTS_LOG_LEVEL` | `info` |
| `WATERAGENTS_HOST` | `127.0.0.1` |
| `WATERAGENTS_PORT` | `8000` |
| `WATERAGENTS_ANALYSIS_TIMEZONE` | `America/Chicago` |
| `WATERAGENTS_MODEL_CATALOG` | `[]`; JSON capabilities with only `provider`, `model`, and `roles` (`query_parser` / `waterbody`) |
| `WATERAGENTS_TOPOLOGY_MAX_DISTANCE_KM` | `100` |
| `WATERAGENTS_TOPOLOGY_MAX_HOPS` | `12` |
| `WATERAGENTS_TOPOLOGY_MAX_NODES` | `128` |
| `WATERAGENTS_TOPOLOGY_MAX_CONCURRENCY` | `8` |
| `WATERAGENTS_PREDICTION_BASELINE_HOURS` | `48` |
| `WATERAGENTS_MAX_TARGET_PARAMETERS` | `6` |
| `WATERAGENTS_QUERY_PARSER_TIMEOUT_SECONDS` | `45` |
| `WATERAGENTS_GEOCODING_TIMEOUT_SECONDS` | `10` |
| `WATERAGENTS_METADATA_TIMEOUT_SECONDS` | `15` |
| `WATERAGENTS_PROVIDER_HTTP_MAX_RESPONSE_BYTES` | `4194304` |
| `WATERAGENTS_PROVIDER_HTTP_PAGE_SIZE` | `1000` |
| `WATERAGENTS_PROVIDER_HTTP_MAX_PAGES` | `10` |
| `WATERAGENTS_PROVIDER_HTTP_MAX_RECORDS` | `10000` |
| `WATERAGENTS_PROVIDER_HTTP_RETRY_DELAY_SECONDS` | `0.25` |
| `WATERAGENTS_GOOGLE_GEOCODING_API_KEY` | unset; backend-only secret |
| `WATERAGENTS_USGS_API_KEY` | unset; backend-only optional rate-limit secret |
| `WATERAGENTS_TOPOLOGY_STAGE_TIMEOUT_SECONDS` | `120` |
| `WATERAGENTS_DB_HOST` | `127.0.0.1` |
| `WATERAGENTS_DB_PORT` | `5432` |
| `WATERAGENTS_DB_NAME` | `wateragents` |
| `WATERAGENTS_DB_USER` | `wateragents` |
| `WATERAGENTS_DB_PASSWORD` | required; no default |
| `WATERAGENTS_DB_SSLMODE` | `disable` |
| `WATERAGENTS_DB_SSLROOTCERT` | unset |
| `WATERAGENTS_DB_POOL_SIZE` / `WATERAGENTS_DB_MAX_OVERFLOW` | `5` / `5` |
| `WATERAGENTS_DB_OPERATION_TIMEOUT_SECONDS` | `5` |
| `WATERAGENTS_IDEMPOTENCY_WAIT_TIMEOUT_SECONDS` | `5` |
| `WATERAGENTS_PERSISTENCE_BATCH_ITEMS` | `100` |
| `WATERAGENTS_TRANSCRIPT_INLINE_MAX_BYTES` | `65536` |
| `WATERAGENTS_TRANSCRIPT_PREVIEW_BYTES` | `2048` |
| `WATERAGENTS_LIVE_STATE_GRACE_SECONDS` | `60` |
| `WATERAGENTS_SHUTDOWN_SAVE_GRACE_SECONDS` | `5` |

`WATERAGENTS_TEST_DB_NAME` has no runtime default and is read only from the test/smoke process environment; the formal service neither reads it nor uses it to override `WATERAGENTS_DB_NAME`. Keep it out of `backend/.env`. If persistent test-only configuration becomes necessary, use a separate uncommitted `.env.test` loaded by test tooling only; the service must not load that file.

Run creation requires one catalog selection for each model role. The catalog stores only public model identities and role metadata; endpoints, credentials, API keys, and authorization values are not valid catalog fields. An empty default keeps health/startup available for configuration checks, but create requests are rejected until both roles are configured.

### Local PostgreSQL with Compose

Copy `.env.example` to `.env`, replace the sample password, and leave that file uncommitted. Then run:

```text
docker compose --env-file .env -f compose.yaml up -d --wait
wateragents-db upgrade
wateragents-db check
python -m app
```

Compose publishes PostgreSQL only on loopback and stores data in the named `wateragents-postgres-data` volume. `wateragents-db upgrade` is safe to repeat. `wateragents-db check` only reads `alembic_version`; it never creates or repairs schema and fails clearly for missing, old, newer, unknown, or multiple revisions.

Stop containers while retaining the database:

```text
docker compose --env-file .env -f compose.yaml down
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

Run `wateragents-db upgrade` as an explicit deployment step, followed by `wateragents-db check`, before starting the application. Never grant the application more database privileges than its deployment model requires. Do not run multiple application workers or replicas: the first M3 release intentionally keeps live coordination and persistence ownership in one process.

When moving to another OS or machine, check out the source and committed `backend/uv.lock`, create a fresh environment with either setup path above, enter `backend/`, activate that environment, recreate the uncommitted UTF-8 `.env`, start or select PostgreSQL 17, and run `wateragents-db upgrade` followed by `wateragents-db check`. Never copy an old virtual environment or database data directory between platforms.

Timezone names must be valid IANA identifiers. The project includes `tzdata` so this behaves consistently when the operating system does not provide an IANA timezone database.

At application startup, `wateragents.*` logs use single-line JSON on stderr at `WATERAGENTS_LOG_LEVEL`. They include request/run/session/error IDs, HTTP duration, committed lifecycle events, and redacted exception diagnostics. HTTP access records use route templates; Uvicorn's default access log is disabled so URLs containing credentials or private queries are not logged. Uvicorn server/error and third-party logs retain their own formats. The [M2 API troubleshooting guide](../docs/M2/m2_analysis_session_sse_api.md#8-排障-runbook) remains the historical M2 reference; use the M3 checklist for the current implementation stage.

`WATERAGENTS_LOG_LEVEL=trace` enables application trace logs; the formal entry point uses `debug` for Uvicorn in that case. Other levels pass through unchanged. When invoking Uvicorn directly, use `debug`, `info`, `warning`, `error`, or `critical` for its server log level; Uvicorn trace logs can expose private ASGI scope data. Application trace remains independently available through `WATERAGENTS_LOG_LEVEL`.

If a response stream fails after headers are sent, the application logs the redacted exception and cause chain, then raises a fixed safe error with the request ID and suppresses the original exception chain for server logging. The connection still aborts and the subscription closes; look up `http_stream_failed` by request ID for the diagnostic details.
