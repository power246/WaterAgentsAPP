# WaterAgents backend

The backend accepts waterbody analysis requests, resolves connected waterways, collects monitoring evidence, schedules Agent reasoning, and persists results and transcripts. It also serves the React frontend, HTTP API, and live event streams from one local process. See the [project README](../README.md) for a complete Windows quick start.

| Path | Purpose |
| --- | --- |
| `app/api/` | HTTP routes, event streams, and API contracts. |
| `app/application/` | Query parsing, topology, evidence acquisition, reasoning, and run orchestration. |
| `app/domain/` and `app/ports/` | Waterbody/evidence rules and external dependency interfaces. |
| `app/infrastructure/` | Provider adapters, configuration, PostgreSQL, and frontend hosting. |
| `tests/` and `scripts/` | Backend tests, diagnostics, and smoke checks. |

It requires Python 3.11 or newer and uses the same source and project configuration on macOS, Windows, and Linux.

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

Configure PostgreSQL, run the explicit schema commands, and build the frontend first; the following sections show both local Compose and existing-database setups. The formal entry point rejects a missing `WATERAGENTS_DB_PASSWORD`:

```text
python -m app
```

The defaults are `127.0.0.1:8000` and analysis timezone `America/Chicago`.

The local demo serves the current React frontend and API from that same loopback origin. Build the frontend once from the repository root before starting the backend; `frontend/dist/` is generated locally and is not committed:

```text
npm --prefix frontend ci
npm --prefix frontend run check:api
npm --prefix frontend run build
```

Then start one backend process with `python -m app` from `backend/`. The UI is at `http://127.0.0.1:8000/`; `/api` and both SSE streams stay on the same origin. If the frontend build is missing, the API remains available and `/health` reports `X-Frontend-Available: false`. Settings and Admin state use PostgreSQL; OpenAI/Google user overrides use the OS user credential store. Saving a key never tests a provider connection. The local demo supports one process and one worker.

Check the service from another terminal:

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

The standalone run/SSE, M4 workflow, and M7 topology workflow smokes are deterministic and do not require PostgreSQL or external credentials. The M4 smoke uses the same composition seam as production with fixed model, geocoding, NLDI, WDFN, and Samples fixtures. The M7 smoke exercises production topology scheduling, dependency propagation, summary composition, failure/cancellation convergence, and the maximum-topology boundary with fixed fixtures:

```text
python scripts/run_sse_smoke.py
python scripts/m4_workflow_smoke.py
python scripts/m7_topology_workflow_smoke.py
```

M9's production-composition smoke uses a disposable schema in `WATERAGENTS_TEST_DB_NAME`, the built frontend, a loopback Uvicorn server, and deterministic replacements for external providers and the OS credential store. From the repository root on Windows, after setting the test database name and installing dependencies, run:

```powershell
python -m dotenv -f backend/.env run --no-override -- python -m pytest backend/tests/smoke/test_m9_production_pg.py -q
```

Set `WATERAGENTS_M9_BROWSER_E2E=1` in the test process to include the installed Chrome browser against that same backend server. This is an offline fake-provider check; it does not validate real provider credentials, paid traffic, capacity, or Ubuntu/macOS. The exact M9 verification record is in [M9 Implementation TODO](../docs/M9/m9_implementation_todo.md).

Audit the checked-in 154-item capability manifest offline with `python scripts/audit_parameter_capabilities.py`. The optional `--refresh-official` flag performs a bounded read of the official USGS Water Data OGC API v1 parameter catalog and reports drift; it never modifies or enables manifest entries.

The `python -m app` entry point explicitly uses one Uvicorn worker and ignores `WEB_CONCURRENCY`. On Windows it passes Uvicorn an explicit Selector loop factory compatible with psycopg async connections; macOS and Linux retain Uvicorn's `auto` selection. The run/history smoke gives its independent Uvicorn children the same platform-specific loop choice. Direct `asyncio.run()` callers keep the existing pre-start policy initialization. The entry point disables Uvicorn's default access log and gives active HTTP/SSE connections 2 seconds to finish during shutdown before entering application cleanup. Lifespan connects to PostgreSQL, requires the exact Alembic head, repairs saved non-terminal run/session state as `failed/service_interrupted`, and only then accepts traffic; it never runs DDL or resumes Agents automatically. User cancellation freezes active message prefixes and stores any later safe tail as history-only audit. Terminal live state is released only after workflow quiescence, required persistence, and the configured full grace period; saved history remains readable and SSE returns HTTP 410 afterward.

The `Backend M1-M9` GitHub Actions workflow installs the same `backend/uv.lock`, provisions PostgreSQL 17, runs the complete test suite, the offline M4 audit, and all five smoke checks with Python 3.11 on Ubuntu, macOS, and Windows. Smoke steps also run with unusable environment proxies to verify local connection isolation. The matrix is the reproducible CI entry; platform support is recorded as partial until each remote job has an actual passing result.

## Test

PostgreSQL integration tests are mandatory. Put only the normal `WATERAGENTS_DB_*` host, port, business database name, user, password, and SSL settings in `backend/.env`. Then set `WATERAGENTS_TEST_DB_NAME` to the pre-created test database in the shell that launches tests and smoke checks. Tests use temporary schemas and refuse a missing, empty, or business-identical test database name.

PowerShell:

```powershell
$env:WATERAGENTS_TEST_DB_NAME = 'wateragents_test'
python -m pytest -q
python scripts/m7_topology_workflow_smoke.py
python scripts/health_smoke.py
python scripts/run_history_smoke.py
Remove-Item Env:WATERAGENTS_TEST_DB_NAME
```

bash:

```bash
export WATERAGENTS_TEST_DB_NAME='wateragents_test'
python -m pytest -q
python scripts/m7_topology_workflow_smoke.py
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
| `WATERAGENTS_MODEL_CATALOG` | `[]`; JSON capabilities with `provider`, `model`, and `roles`; configured catalogs require exactly one exclusive `embedding` capability with a cosine `embedding_profile` including dimensions, input-token limit, threshold, and schema versions, while every `waterbody` model requires its exact token limits |
| `WATERAGENTS_MEMORY_ENABLED` | `true`; set `false` to disable historical memory and its background indexing. The embedding catalog entry remains required. Published guidelines use a separate index and can still use embedding when historical memory is disabled. |
| `WATERAGENTS_TOPOLOGY_MAX_DISTANCE_KM` | `500` |
| `WATERAGENTS_TOPOLOGY_MAX_HOPS` | `48` |
| `WATERAGENTS_TOPOLOGY_MAX_NODES` | `128` |
| `WATERAGENTS_TOPOLOGY_MAX_CONCURRENCY` | `8` |
| `WATERAGENTS_NLDI_REQUEST_INTERVAL_SECONDS` | `1.5`; minimum time between NLDI request attempts, valid range `0..10` |
| `WATERAGENTS_WATERBODY_WORKFLOW_MAX_CONCURRENCY` | `4`; shared waterbody-session limit, valid range `1..32` |
| `WATERAGENTS_RUN_SUMMARY_TIMEOUT_SECONDS` | `120`; one run-summary request deadline, valid range `(0, 300]` |
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
| `WATERAGENTS_OPENAI_API_KEY` | unset; required backend-only parser secret |
| `WATERAGENTS_OPENAI_BASE_URL` | `https://api.openai.com/v1` |
| `WATERAGENTS_EMBEDDING_BASE_URL` | unset; optional independent OpenAI-compatible embedding base URL; configure together with `WATERAGENTS_EMBEDDING_API_KEY` |
| `WATERAGENTS_EMBEDDING_API_KEY` | unset; backend-only embedding secret from deployment configuration, paired with the independent URL |
| `WATERAGENTS_TOPOLOGY_STAGE_TIMEOUT_SECONDS` | `1200`; total topology-building deadline, valid range `(0, 3600]` |
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

Each waterbody token limit belongs to that exact provider/model entry. Copy the selected model's documented limits; do not reuse one provider-wide default for multiple models.

`WATERAGENTS_TEST_DB_NAME` has no runtime default and is read only from the test/smoke process environment; the formal service neither reads it nor uses it to override `WATERAGENTS_DB_NAME`. Keep it out of `backend/.env`. If persistent test-only configuration becomes necessary, use a separate uncommitted `.env.test` loaded by test tooling only; the service must not load that file.

Production startup validates the complete topology workflow and requires at least one `query_parser` catalog entry (currently with provider `openai`), one `waterbody` entry with explicit token limits, and one supported embedding profile. It does not permanently capture the OpenAI or Google key at startup: each run leases its effective user-store override or deployment fallback before creation, and each background embedding attempt leases its key when claimed. Missing effective credentials prevent the relevant run or attempt. The run summarizer uses the run's exact waterbody model and catalog token limits; it has no separate model setting or tools. The catalog stores only public model identities, roles, and waterbody token limits; endpoints, credentials, API keys, and authorization values are not valid catalog fields. Tests and deterministic smokes may inject an explicit workflow through the application factory.

### Independent embedding service

To keep the current analysis provider and use a separate OpenAI-compatible embedding service, add both entries to the uncommitted `backend/.env`:

```dotenv
WATERAGENTS_EMBEDDING_BASE_URL='https://generativelanguage.googleapis.com/v1beta/openai/'
WATERAGENTS_EMBEDDING_API_KEY='your-local-gemini-api-key'
```

Create a Gemini API key in [Google AI Studio](https://aistudio.google.com/api-keys). Google documents the `/embeddings` compatibility endpoint in its [OpenAI compatibility guide](https://ai.google.dev/gemini-api/docs/openai/#embeddings). Leave the existing analysis `WATERAGENTS_OPENAI_*` values unchanged. Do not use the Google Geocoding key for this service.

In `WATERAGENTS_MODEL_CATALOG`, replace only the embedding capability with the service's model/profile. For Google's text-only `gemini-embedding-001`, an example is:

```json
{
  "provider": "openai",
  "model": "gemini-embedding-001",
  "roles": ["embedding"],
  "embedding_profile": {
    "provider": "openai",
    "model": "gemini-embedding-001",
    "output_dimensions": 1536,
    "max_input_tokens": 2048,
    "distance_metric": "cosine",
    "semantic_min_similarity": 0.79,
    "canonical_query_schema_version": 1,
    "canonical_template_version": 1,
    "projection_schema_version": 1
  }
}
```

说明书索引当前采用关系模板 v3：安全正文按完整段落及标题边界生成关系单元，保留标题层级、条件与否定；完全相同的单元共享向量，并以多对多关联指向不可变文章版本。标题/摘要保留作简介，命中后仍读取完整正文与出处。四份 pH 说明书已于 2026-10-21 获用户明确批准并以 v1 纳入内置资源，逐项批准记录位于 bundle manifest；在上述 Google 示例下分别生成 6／6／7／6 个片段，最长完整 wire 输入分别为 623／680／675／624 UTF-8 字节，均通过 2048 字节保守门禁。门禁检查包含角色指令的完整输入，单个超长段落仍会明确准备失败，不截断条件或提高模型限制。

上例的 0.79 门槛仅针对当前 Google `gemini-embedding-001`、1536 维、v3 和这四篇内容校准：原 12 题在默认 0.35 下有 2 个负例误命中；固定为 0.79 后，原集和另行编写的 8 题中，14 个正例均首位命中、6 个负例均未返回。原始误命中记录保留。门槛不是其他模型/语料的通用默认值；`GuidelinePolicy` 默认仍为 0.35，部署通过 `WATERAGENTS_GUIDELINE_POLICY` 指定。仅改 cutoff 不改变向量输入/profile，不需重建 generation；更换模型、模板或角色指令须按原流程重建。扩大资料库后需重新核对，不能以小样本证明普遍检索质量。

补充的任务上下文/多方面输入集 `tests/fixtures/guidelines/ph_retrieval_context_cases.json` 有 6 个正例、6 个负例：期望主题均进入 top-3，4/6 首位命中，负例均未返回；两条保留的真实模型改写却仍未命中。最终三个 holdout 配对全部完成，两组实际提供并引用批准内容，一组未命中。六个评分窗口均为范围，没有合格 pH anchor；Potomac 第二天两臂都未覆盖观测均值。这个结果验证链路和边界，尚不能证明估值精度改善，详见现有 guideline TODO。

2026-10-22 已修正离线评估的阈值流程，并以真实 Google embedding 重算上述 14 个输入（18 次供应商请求，含四篇内容准备），本组命中计数不变。v3 的 cutoff 作用于关系片段，返回的多方面聚合分数可以低于 cutoff；评估直接使用线上 `rank_guidelines()` 的 cutoff/top-k 返回值，不再对聚合分数重复过滤。报告中的 `ranking/selected` 表示实际筛选结果，`unfiltered_ranking` 仅用于无门槛诊断。旧报告保留，原 12+8 个单查询本轮未重跑。

真实配对需完整 UTC 采集日输入，评分仍限原六小时窗口。目标站整天的 pH 均被隔离，不能把相邻窗口读数用于补值。下列命令从 `backend/` 在已加载服务配置、并设置进程级 `WATERAGENTS_TEST_DB_NAME` 的环境执行；只创建临时测试 schema，输出目录必须尚不存在。`--allow-live` 会向已配置服务发出真实模型/embedding 请求；默认不运行：

```powershell
.venv/Scripts/python.exe -m scripts.guideline_live_pairs --dataset tests/fixtures/guidelines/ph_holdout_acquisition_cases.json --metadata-root tests/fixtures/guidelines/ph_sites --output ../.tmp/my-live-pairs --allow-live
.venv/Scripts/python.exe -m scripts.guideline_live_review --dataset tests/fixtures/guidelines/ph_holdout_acquisition_cases.json --runs-root ../.tmp/my-live-pairs --output ../.tmp/my-live-comparison.json
```

首次实际模型请求前，查询 skill 通过一次 `search_guidelines(query, aspects)` 提交专业改写和 1–3 个原问题侧面；后端做一次批量 embedding，固定范围/profile/top-k（最多 3 份），按最佳片段匹配及独立侧面覆盖排序，后续阶段复用。近同查询向量去重，每侧面最多取一个最佳匹配，同一片段不重复贡献；不会仅靠文章片段多就堆高排名。空库和零模型路径不新增该调用，实际额外回合计入调用数和用量。升级 profile 仍通过完整 building generation 原子切换；显式旧 v1/v2 generation 及历史全文继续可读。

查询改写自身的模型超时按参考不可用记录，原推理可以继续且不重试查询；用户取消、会话到期或引用记录写入失败仍终止原流程。完整正文、delivery/usage 与既有数值资格规则继续保持。

合成语义对比的源码位于 `tests/fixtures/guidelines/synthetic_relation_corpus.json` 和 `tests/guideline_relation_benchmark.py`，不属于生产资源。可在已配置独立测试库的进程中设置 `WATERAGENTS_RELATION_LIVE_E2E=1`，运行 `tests/application/guidelines/test_relation_retrieval.py::test_real_embedding_synthetic_relation_comparison`；它只向真实 embedding 发送虚构内容，比较摘要单查询、片段单查询和片段多侧面，结束后清理测试 schema。可选 `WATERAGENTS_RELATION_REPORT_PATH` 指定本地 JSON 结果路径；不向 `.env` 写测试库名、不导入业务库。结果与限制见现有 guideline TODO；合成查询使用人工编写侧面，不代表真实分析模型改写或实测 pH 验收。

The pair applies to both historical-memory and guideline embeddings: run query adapters keep their captured key, and background attempts resolve the embedding connection at their own boundary. Independent credentials come only from deployment configuration and require a restart to change; the Settings OpenAI user override continues to control analysis, and does not override the independent embedding key. With both entries unset, embeddings retain the effective OpenAI credential lease, including its OS-store override. A partially configured pair is rejected before any provider request; an embedding failure never retries against the analysis endpoint.

At the exact HTTPS Google compatibility endpoint shown above, an omitted first response `index` means zero. All subsequent indices remain mandatory and must match input order; explicit invalid indices, model/count/dimension mismatches and invalid vector values are rejected. Other endpoints retain mandatory indices for every result.

Restart after changing configuration. Changing the model/profile rebuilds the guideline generation; historical memory retains its persisted generation until the existing Admin profile handoff/rebuild is explicitly applied. Old vectors, local documents and run snapshots are preserved; no database reset is required. A successful provider connection does not establish guideline retrieval relevance or scientific accuracy.

### Local PostgreSQL with Compose

Copy `.env.example` to `.env`, replace the sample password, and leave that file uncommitted. Replace the placeholder model catalog and provider settings before running real analyses. Then run:

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

Run `wateragents-db upgrade` as an explicit deployment step, followed by `wateragents-db check`, before starting the application. Never grant the application more database privileges than its deployment model requires. Do not run multiple application workers or replicas: the scheduler, concurrency limiter, run-local upstream-result registry, live cancellation, and workflow quiescence are owned by one process and are not shared across workers.

When moving to another OS or machine, check out the source and committed `backend/uv.lock`, create a fresh environment with either setup path above, enter `backend/`, activate that environment, recreate the uncommitted UTF-8 `.env`, start or select PostgreSQL 17, and run `wateragents-db upgrade` followed by `wateragents-db check`. Never copy an old virtual environment or database data directory between platforms.

Timezone names must be valid IANA identifiers. The project includes `tzdata` so this behaves consistently when the operating system does not provide an IANA timezone database.

At application startup, `wateragents.*` logs use single-line JSON on stderr at `WATERAGENTS_LOG_LEVEL`. They include request/run/session/error IDs, HTTP duration, committed lifecycle events, and redacted exception diagnostics. HTTP access records use route templates; Uvicorn's default access log is disabled so URLs containing credentials or private queries are not logged. Uvicorn server/error and third-party logs retain their own formats. The [M2 API troubleshooting guide](../docs/M2/m2_analysis_session_sse_api.md#8-排障-runbook) remains the historical M2 reference; use the M3 checklist for the current implementation stage.

`WATERAGENTS_LOG_LEVEL=trace` enables application trace logs; the formal entry point uses `debug` for Uvicorn in that case. Other levels pass through unchanged. When invoking Uvicorn directly, use `debug`, `info`, `warning`, `error`, or `critical` for its server log level; Uvicorn trace logs can expose private ASGI scope data. Application trace remains independently available through `WATERAGENTS_LOG_LEVEL`.

If a response stream fails after headers are sent, the application logs the redacted exception and cause chain, then raises a fixed safe error with the request ID and suppresses the original exception chain for server logging. The connection still aborts and the subscription closes; look up `http_stream_failed` by request ID for the diagnostic details.
