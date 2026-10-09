# M9 Settings/Keys/Admin + Frontend Implementation TODO

更新日期：2026-10-08

> 当前状态：M9 产品、Frontend Experience、Policy Catalog、API 与实施设计已经冻结；Milestone 0–9 的本机实现及阶段验证已完成并分别保留本地提交。真实 provider、真实 OS credential backend、容量与 Ubuntu/macOS 验证仍按实际证据保持 partial / not run。

本清单执行以下权威文档：

- [M9 总设计 D01–D35](m9_settings_admin_demo_design.md)
- [M9 Frontend Experience](m9_frontend_experience_design.md)
- [M9 Settings/Admin Policy Catalog](m9_settings_admin_policy_catalog.md)
- [M9 Settings/Admin API](m9_settings_admin_api.md)
- [M9 实施设计](m9_implementation_design.md)
- [跨 Milestone M9 契约](../deferred_milestone_contracts.md#11-m9settingskeysadmin--demo-closure)
- [Frontend Integration Contracts](../frontend_integration_contracts.md)
- [M8 Implementation TODO 的最终接力证据](../M8/m8_implementation_todo.md)
- [Docker PostgreSQL 快速指南](../docker_postgresql_quickstart.md)

若本清单与权威设计或当前 production contract 冲突，coding agent 不得自行选成本最低的一项、不得用 feature flag/兼容层同时保留两套行为，也不得用 `Any`、任意 `dict`、unchecked cast 或 test-only production switch 绕过。应暂停当前阶段，查明当前唯一 V1 事实，并在同一变更中同步设计、API、Policy、TODO 与测试后再继续。内部函数拆分、单一用途文件合并、React component 粒度和 fixture 组织可由实现者决定，但不能改变新旧系统隔离、future-run-only settings、OS credential store、per-run/attempt lease、unit policy、Admin ownership、两层 SSE、原时区、七种地图视觉、single-process/worker 或 local same-origin 边界。

## 阶段与设计导航

| 实施阶段 | 主要设计依据 | 阶段产物 |
|---|---|---|
| [Milestone 0：基线、接缝与红线](#milestone-0基线接缝与红线) | [实施设计 §1–3](m9_implementation_design.md#1-当前接入点与缺口) | 可复现基线、真实影响表、red gates、原子更新清单 |
| [Milestone 1：当前唯一 V1 类型、ports 与 wire contract](#milestone-1当前唯一-v1-类型ports-与-wire-contract) | [实施设计 §4](m9_implementation_design.md#4-domainapplication-types)；[API](m9_settings_admin_api.md) | typed domain/application contracts、ports、errors、OpenAPI 目标模型 |
| [Milestone 2：PostgreSQL migration 与 repositories](#milestone-2postgresql-migration-与-repositories) | [实施设计 §5](m9_implementation_design.md#5-postgresql-schema) | settings/unit/tombstone/operation schema、CAS 与原子 repositories |
| [Milestone 3：Settings、创建快照与浏览器写保护](#milestone-3settings创建快照与浏览器写保护) | [Policy §2–6/§10](m9_settings_admin_policy_catalog.md#2-settings-分类)；[API §4](m9_settings_admin_api.md#4-settings-api) | SettingsService、settings API、future-run snapshot、same-origin mutation guard |
| [Milestone 4：OS credentials 与 per-run/attempt provider runtime](#milestone-4os-credentials-与-per-runattempt-provider-runtime) | [实施设计 §6–8/§10](m9_implementation_design.md#6-os-credential-adapter)；[API §6](m9_settings_admin_api.md#6-key-api) | OS key store、masked key API、lease、per-run bundle、background attempt resolver |
| [Milestone 5：全局单位 policy 与 session override](#milestone-5全局单位-policy-与-session-override) | [Policy §5](m9_settings_admin_policy_catalog.md#5-unit-policy)；[实施设计 §9](m9_implementation_design.md#9-unit-intent-与-reasoning-接入) | versioned unit policy、preview/confirm、typed intent、reasoning/validator integration |
| [Milestone 6：Admin operation、history tombstone 与 RAG 运维](#milestone-6admin-operationhistory-tombstone-与-rag-运维) | [Policy §11–16](m9_settings_admin_policy_catalog.md#11-preview-与-confirmation)；[API §7–11](m9_settings_admin_api.md#7-admin-read-api) | preview registry、persistent operations、history cleanup/tombstone、M8 maintenance routes |
| [Milestone 7：Frontend foundation、实时分析与 History](#milestone-7frontend-foundation实时分析与-history) | [Frontend Experience §2–9/§13](m9_frontend_experience_design.md#2-信息架构与-url) | React/Vite app、typed HTTP/SSE、三栏布局、analysis/map/detail/history |
| [Milestone 8：Frontend Settings/Admin 与 production hosting](#milestone-8frontend-settingsadmin-与-production-hosting) | [Frontend Experience §10–14](m9_frontend_experience_design.md#10-settings-与-keys)；[实施设计 §12–13](m9_implementation_design.md#12-static-frontend-hosting) | Settings/Keys/Admin UI、polling、static hosting、dev proxy、browser integration tests |
| [Milestone 9：端到端验收、契约审计与交付证据](#milestone-9端到端验收契约审计与交付证据) | [实施设计 §16–18](m9_implementation_design.md#16-testing-strategy) | fake/PG/browser E2E、全回归、contract matrix、真实边界实录 |

## Coding agent 持续执行原则（非完成项）

以下规范贯穿所有阶段，不作为完成状态复选框：

- 只以当前 `backend/`、`frontend/`、`docs/M1`–`docs/M9` 和项目级当前契约为实现输入。`backend_bk/`、`frontend_bk/` 与旧契约属于独立归档；旧 UI 的一次人工调研结论已经冻结为 M9 D18/D19，后续不得再次读取、导入、复制、适配、运行或把归档当测试 oracle。
- 开始每个 Milestone 前记录 branch、HEAD、`origin/main`、merge-base、upstream、tracked/unstaged/untracked 和 permission-limited 路径；保护已有用户改动，不清理历史 pytest 目录、不顺手格式化无关文件。
- 一个 Milestone 只交付本表的一项可观察目标。先补精确失败测试或 contract gate，再做最小 production 实现，再跑 focused gate；阶段出口未满足时不得进入下一阶段或勾选项目。
- 每个 Milestone 原则上形成一个可独立审查的提交。未经用户要求不 push、不创建 PR、不改写历史；提交前列出实际文件范围并确认没有 secret、`frontend/dist/`、Playwright artifact、数据库 volume、pytest cache 或 provider response。
- M9 是 M1–M9 同一系统的最后一个内部实施切片，不建立“M9 兼容模式”。直接原子更新当前唯一 V1 types、schema、OpenAPI、runtime、fixtures 与消费者；不增加 V2/read union、双写、旧 route、WebSocket、第三 SSE 或归档 frontend adapter。
- 第一版是 `127.0.0.1` 本地单用户、单 process、单 worker Web 应用。不要预建 authentication、remote bind、多租户、distributed queue、leader election、Electron/Tauri、installer、auto-update、system tray 或 plugin system。
- local-only 不等于 mutation 无保护。写请求必须遵守 strict JSON/DTO、loopback/same-origin/Fetch Metadata policy；无 `Origin` 的明确本地 CLI 可以按冻结规则工作，但 cross-site browser mutation 必须拒绝。
- mutable settings 只影响未来 run；创建时固化 timezone、model/memory/resolution 与 unit policy identity。历史永远显示原 IANA timezone；旧记录缺字段显示 legacy/unknown，不能用当前默认值回填。
- secret 只存在于 deployment `SecretStr`、OS user credential store、短生命周期 lease 和 provider request private boundary。不得进入 PostgreSQL、transcript、OpenAPI、public DTO、URL、DOM、localStorage、console、日志、metrics、fixture、snapshot 或测试 artifact。
- Key UI 只允许 `openai` 与 `google_geocoding`。USGS 继续是 deployment-only optional config；不得因 `AppSettings` 中已有字段就自动暴露。保存 key 不联网，只有用户点击 `Test connection` 或真实分析/显式 smoke 才允许外部请求。
- provider adapter 不得在 startup 永久捕获会变化的 key。run 使用创建时取得的 immutable credential lease；M8 background embedding 每个 claimed attempt 取得一次 lease，attempt 中不切换，下一 attempt 才看新值。
- 全局单位变更必须 preview/confirm 并生成 append-only version；普通 Settings PUT 不得修改 active unit policy。session override 只影响当次分析，不能定义公式、扩大参数或改写原 evidence/source facts。
- Admin destructive action 固定为 server preview → token/phrase → idempotent execute → persisted operation。preview token 只在有界进程内 registry；重启后要求重新 preview，不为短期 token 建表。
- history、RAG index/generation 和 credentials 是三个隔离资源图。history cleanup 不删除 keys/settings/unit versions；index-only cleanup 不删除 transcript；key 删除不声称清理历史或向量。
- 当前没有持久化 evidence cache。不得实现 `Clear evidence cache` route/button、伪 byte count、伪 last-cleared 或把 transcript source records 改名成 cache。`CachePort` 存在不等于有可清理存储。
- run 删除与 create idempotency tombstone 必须同事务；旧 key 同 bytes 返回 410，不同 bytes 返回 409。不得释放旧 key、自动生成新 key重发或保存原 query/body/receipt 到 tombstone。
- Admin 必须复用 production composition 中同一个 `RAGMaintenanceOperations` 和 reader/repository graph；不得 new 第二套 maintenance owner。被 retained run 消费的 source 只能稳定 conflict，或在用户明确确认后级联删除整个 dependent run。
- 前端只消费相对 `/api`、当前 REST 与现有两层 SSE。GET/SSE、run/session/message/persistence/UI draft 是不同状态；不得维护一个混合 `analysisStatus`，不得靠时间戳猜更新顺序。
- session SSE 以 SSE integer id 去重，不能用 `item_id` 丢弃不同 delta；run SSE 使用 replacement snapshot。HTTP EOF、`stream_end`、业务 terminal 与 persistence complete 是四件事，不得互相替代。
- 地图颜色是前端对真实 topology/session facts 的映射。无 session 的 pending/cancelled-before-start 节点不得伪造空 session；running 蓝色呼吸只在外圈，第一版不新增 reduced-motion setting，截图测试冻结动画。
- frontend server state 优先使用 feature reducer/context；不要先引入 Redux、全局 event bus、第二套 domain framework 或 design system。只有现有状态工具无法保持 identity 时，才在同一设计变更中证明并新增依赖。
- external provider smoke 不是单元测试前置。先用 deterministic fake 完成全部功能；任何联网、凭据或可能计费的请求必须 preflight 并获得用户明确授权，未运行就记录 `not run`。
- 完成记录必须写明 production paths、migration revision、命令、环境、实际 pass/fail/skip/not-run 数、失败与修复、数据库隔离、provider/platform evidence、docs 状态、commit SHA 和剩余风险。不得复制旧阶段数字或用 exit 0、HTTP 200、页面能打开、`stream_end`、表存在来替代业务断言。

## M9 完成标准

- [x] 当前唯一 V1 能严格表达 mutable defaults、safe catalog、credential status/lease、unit policy/override、Admin preview/operation 和 idempotency tombstone；producer/consumer/fixtures/OpenAPI/前端类型原子一致。
- [x] 现有 M8 database 可通过一个新 Alembic head 升级；fresh install、upgrade、schema check、settings CAS、unit append-only、operation idempotency、tombstone transaction 和 restart 使用专用 PostgreSQL 验证。
- [x] Settings GET/PUT、unit preview/confirm、key list/save/delete/test、Admin read/preview/execute/poll routes 与冻结 API 一致；unknown field/content type/origin/error/status/OpenAPI 都有测试。
- [x] settings/key 更新只影响未来 run/attempt；在途 run 保留 configuration/credential leases，历史明确显示原 timezone/unit policy，secret 不进入任何公开或持久化边界。
- [x] M4/M6/M8 runtime 不再从 startup 固定 secret构造长期 provider adapter；per-run bundle、background attempt lease、cancel/shutdown 和 deployment fallback 可执行验证。
- [x] query unit intent、global unit policy 与 session override 经 registry、dimension、sealed parameter scope、reasoning applicability 和 validator闭环；conversion不绕过现有 evidence policy。
- [x] Admin preview/confirmation/operation、restart reconciliation、history dependency conflict/whole-run cascade、tombstone、generation handoff/retry/delete 都复用现有 owner并无 dangling refs。
- [x] `frontend/` 是唯一生产前端，React/TypeScript/Vite、OpenAPI types、typed HTTP/fetch-SSE、三栏布局、analysis/map/detail/history/settings/admin 完整；不读取或构建 `frontend_bk/`。
- [x] run/session/message/persistence 状态、七种地图视觉、原时区、three detail tabs、历史分页、masked keys、settings conflict、Admin polling与操作恢复都有 unit/integration/browser evidence。
- [x] backend 同 origin托管 production build；API 404 不被 SPA fallback 吞掉，SSE不缓冲，hashed assets/index cache、path traversal、dist missing 与 Vite proxy 均有测试。
- [x] deterministic fake browser E2E、专用 PostgreSQL/restart、M1–M9 full backend regression、frontend unit/build/E2E、architecture/OpenAPI/docs/secret audits 有实际证据；真实 provider、容量与三平台按事实记账。
- [x] GLOBAL-C01–C11、M9-C01–C11、M9-A01–A08 的 contract matrix 填入实际 implementation/test/docs evidence；`partial`/`not run` 不标记为已通过。

## Milestone 0：基线、接缝与红线

### 工作

- [x] 记录 branch、HEAD、`origin/main`/upstream/merge-base、tracked/unstaged/untracked/permission-limited 状态、Python/Node/npm/浏览器版本、PostgreSQL migration head 和现有测试入口；保护当前 M9 文档与所有用户改动。
- [x] 确认 `frontend/` 当前真实状态、`.gitignore`、构建产物边界和 `frontend_bk/` 隔离；不得因为空目录存在就声称前端已初始化。
- [x] 追踪 production `create_app`/lifespan、settings/catalog、M4/M5/M6/M7/M8 runtime、analysis create、history/SSE、persistence retry 与 shutdown 的真实调用链，标出 M9 SettingsRuntime、per-run factory、Admin runner 和 static responder 的唯一插入点。
- [x] 追踪 `AppSettings` 三类 key在 M4/M5/M6/M8 的读取位置、HTTP client/header ownership、run/provider 生命周期和 M8 worker attempt，形成需要原子替换的 caller/builder/fake/smoke 列表。
- [x] 追踪当前 `AnalysisConfigurationSnapshot` 从 create body/default resolution 到 writer、PostgreSQL、history/API/SSE/M4–M8 consumer 的完整链，形成 timezone/unit policy向前演进清单。
- [x] 盘点 `KeyStorePort`、parameter/unit registry、`RAGMaintenanceOperations`、cleanup transaction、idempotency receipt、error mapper、OpenAPI 和 safe-log redaction 的真实能力与缺口。
- [x] 记录 backend non-PG、专用 PostgreSQL、architecture/OpenAPI/M8 smoke 基线；环境/ACL/缺少 Node 浏览器等限制如实标为 failed/skipped/not run/permission-limited。
- [x] 增加 architecture red gates：production 不 import archive；M9 domain/application 不 import FastAPI/SQLAlchemy/keyring/React；无第三 SSE/WebSocket/direct RAG search route/evidence-cache route/desktop shell/auth/multi-worker queue；API route不直连 repository/keyring。
- [x] 增加失败 contract tests，精确锁定当前缺失的 settings/key/admin routes、snapshot unit identity、tombstone 410、OS-store projection、operation status与 static hosting，不因 import/fixture 拼写错误失败。

### Tests

- [x] 基线报告区分现有通过、现有失败、PostgreSQL、frontend、browser、provider、三平台和 permission-limited；不把没有 `frontend/package.json` 记为 test pass。
- [x] architecture tests 会拒绝 archive import、旧 frontend build、第三 SSE/WebSocket、route直连 infrastructure、secret persistence、evidence-cache surface、remote/multi-user/desktop overbuild。
- [x] red contract tests 在业务实现前只因冻结能力缺失而失败，并注明后续负责 Milestone；现有 M1–M8 tests 不被改成适应未实现行为。
- [x] production impact表覆盖 code、tests、scripts、OpenAPI、docs 和 composition builders，没有遗漏长期捕获 secret 的 caller或第二套 cleanup owner。

### Verify

- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture backend/tests/api/test_openapi_contract.py backend/tests/infrastructure/test_m4_runtime.py backend/tests/infrastructure/test_m6_runtime.py backend/tests/infrastructure/test_m8_runtime.py -q`
- [x] 使用专用测试数据库运行当前 persistence/RAG baseline，并记录实际 `WATERAGENTS_TEST_DB_NAME`、migration head和 basetemp。
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests -q`（PostgreSQL/外部 provider按环境拆分并分别记账）
- [x] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [x] `git diff --check`

阶段出口：production 插入点、所有 settings/secret/snapshot/admin/frontend consumers、数据库缺口与可复现基线已经记录；red tests 精确描述 M9 缺口，本阶段不实现新 route、migration、keyring、runtime factory 或 frontend。

提交边界：只允许 architecture guards、失败 contract tests、基线/影响记录与必要的测试命名澄清；不得提前增加业务实现或依赖。

完成记录：实施后按文末模板填写。

### M0 基线与影响记录（2026-10-08）

- Git：从最新 `origin/main=cf7eed70aa43fd6b4a2303a7e0b933804a113735` 新建 `codex/m9-milestone-0`，起点 HEAD/merge-base 同 SHA，upstream 为 `origin/main`；`git fetch` 与 `git ls-remote` 均已核对。切分支前目标源码/docs 路径 tracked、unstaged、untracked 均为 0；`target/`、`backend/target/` 等历史 pytest 目录有 ACL 限制，不能把全仓扫描等同于完全干净。未创建 worktree，也未触碰归档或旧测试缓存。
- 环境：backend venv Python 3.12.14（系统 Python 3.13.2），Node 24.21.0、npm 11.19.0；本机 Chrome 154.0.8037.98、Edge 154.0.4258.62。`frontend/` 无 `package.json`、lockfile、源码或 build；`.gitignore` 已排除 `node_modules/`、`dist/` 等，后续 Playwright artifact 仍需单独审查。旧 `frontend_bk/` 不进入生产/构建。当前 Alembic 唯一 head 为 `20261007_01`；Docker pgvector PostgreSQL 17 容器 healthy，PG 测试使用独立 `wateragents_test` 的临时 schema。

| 接缝与原子替换范围 | 当前生产事实 / M9 插入点 | 同步消费者 |
|---|---|---|
| composition 与生命周期 | `backend/app/main.py:create_app` lifespan 先校验 DB/repair，再建 M4 workflow、coordinator/reader、M8、M5、M6、M7，启动 M8 worker；shutdown 依次停 M8/coordinator/engine。SettingsRuntime 应接在 DB 校验后；per-run resolver/factory 接 create 前和 run 生命周期；Admin runner 复用 `m8_runtime.operations`；API router 后才接非 `/api` static responder。 | `backend/tests/application/test_run_workflow.py`、`test_milestone8_runtime.py`、`backend/tests/infrastructure/test_m4_runtime.py`、`test_m5_runtime.py`、`test_m6_runtime.py`、`test_m8_runtime.py`、`test_m7_runtime.py`、`backend/tests/smoke/test_m8_memory_rag_smoke.py`。 |
| secret / HTTP ownership | `AppSettings` 的 OpenAI、Google、USGS 均为 `SecretStr`。`runtime/m4.py` 在 startup 取 OpenAI/Google/USGS；`runtime/m5.py` 取 deployment-only USGS；`runtime/m6.py` 在 startup 取 OpenAI；`runtime/m8.py` 在 startup 取 OpenAI，search 与 worker 共用 embedding adapter。各 adapter 自持 key并在调用边界写 Authorization、Google URL key或 USGS `X-Api-Key`，`ProviderHttpClient` 不应设置全局 secret header。M9 需替换 M4/M6/M8 的长期捕获、给 run 与 M8 claimed attempt 各自 lease；USGS 保持 deployment-only。 | `backend/tests/m6_reasoning_support.py`、各 runtime tests、`backend/tests/smoke/test_m8_memory_rag_smoke.py`，`backend/scripts/m4_workflow_smoke.py`、`m6_fixture_runtime.py`、`m6_provider_smoke.py`、`m5_provider_smoke.py`。真实 provider smoke 本阶段未运行。 |
| configuration / history / SSE | `api/http/analysis_runs.py:create_run` 把原始 body交 `RunCoordinator.create_run`；`AnalysisRequestV1.to_configuration_snapshot` 用固定 `analysis_timezone`、默认/显式 resolution、model/memory生成当前 `AnalysisConfigurationSnapshot`。coordinator 写 `RunCreationCandidate`，Postgres store保存 typed run-created payload；`TranscriptReader`、history API、run/session SSE再投影。M9 在 create 时固化 timezone、unit version/digest并同步 reader、startup repair、M4/M6/M7/M8消费者；旧记录投影 legacy/unknown。 | `backend/app/application/analysis_runs/{models,coordinator,startup_repair}.py`、`application/transcripts/{models,reader,read_models}.py`、`infrastructure/persistence/{store,records,schema}.py`、`api/{schemas/http/sse}` 与对应 run/history/events/persistence tests。 |
| 单位与管理 | `ParameterCapabilityRegistry`/manifest 是单位 authority，`convert_evidence_unit` 已用 registry target；无 global version。`KeyStorePort` 只有 generic get/set/delete，无 OS adapter/status。M8 `RAGMaintenanceOperations` 只在 `runtime/m8.py` 组合一次，`rag_repository.py:delete_history_runs` 已同事务处理 consumption 与 run facts，但尚未写 tombstone；live create receipt在 `analysis_runs`。 | M1–M6 需同改 ports、schema/migration、maintenance/repository、idempotency create、error mapper `_SAFE_FIELDS` 与 safe-log/private-values；不得 new 第二个 cleanup owner。 |
| wire、前端、文档 | 当前 `main.py` 仅注册 health/analysis router并补充 OpenAPI schema；现有两条 SSE，history/cancel/retry 保持原路径。`frontend/` 尚未初始化，backend 未托管 static。 | `backend/tests/api/test_openapi_contract.py` 有精确旧路径集合，M1/M3 扩 route时须同步；`docs/M9` 的设计/API/Policy/Frontend、`docs/frontend_integration_contracts.md`、`backend/README.md`、`.github/workflows/backend-m1.yml` 与未来前端生成类型/脚本需随各阶段更新。 |

新增 `backend/tests/architecture/test_m9_boundaries.py` 为 archive、依赖方向、route/持久化/前端构建与单 worker 护栏；`backend/tests/api/test_m9_red_contract.py` 用 strict xfail 锁住 Settings(M1/3)、OS key(M1/4)、Admin operation(M1/6)、unit snapshot(M1/3)、deleted receipt 410(M1/6)、static hosting(M8)。`--runxfail` 应恰好因这些缺失断言而失败；后续负责阶段使对应项转绿并移除 xfail。

验证记录（Windows，本机，命令均从仓库根目录运行）：

| Gate | 实际结果 |
|---|---|
| 原有 architecture + OpenAPI + M4/M6/M8 runtime 命令；`--basetemp=backend/target/m9-m0-architecture-elevated-20261008` | 202 passed。 |
| `WATERAGENTS_TEST_DB_NAME=wateragents_test`，项目 venv 的 `python -m dotenv -f backend/.env run --no-override -- ... pytest backend/tests/persistence backend/tests/smoke/test_m8_memory_rag_smoke.py -q`；`--basetemp=backend/target/m9-m0-pg-elevated-20261008` | 98 passed；随机临时 schema 升级到 `20261007_01` 后清理，未使用业务库。 |
| 非 PG：`pytest backend/tests --ignore=backend/tests/persistence --ignore=backend/tests/smoke/test_m8_memory_rag_smoke.py -q`；`--basetemp=backend/target/m9-m0-full-nonpg-final-elevated-20261008` | 2164 passed、6 xfailed、1 既有 StarletteDeprecationWarning。 |
| 全量：同上设置专用测试库，经项目 venv `dotenv` 运行 `pytest backend/tests -q`；`--basetemp=backend/target/m9-m0-full-pg-final-elevated-20261008` | 2262 passed、6 xfailed、1 同类 warning；6 项 strict xfail 仅为 M9 后续阶段契约。 |
| M9 focused：`pytest backend/tests/architecture/test_archive_isolation.py backend/tests/architecture/test_m9_boundaries.py backend/tests/api/test_m9_red_contract.py -q`；`--basetemp=backend/target/m9-m0-archive-repair-elevated-20261008` | 13 passed、6 xfailed；单跑 red contract `--runxfail` 为 6 failed，均为缺失能力断言。 |
| `python -B -m compileall -q backend/app backend/scripts`；`git diff --check` | 均 exit 0。 |

失败与修复：受限运行的 pytest 临时目录触发 WinError 5，改用上述可访问的 basetemp/测试权限；无 DB 环境的首次全量运行 2203 passed、37 failed、18 errors，失败集中在缺少专用测试库配置，随后按专用 PG 命令重跑。首次 PG 全量 2261 passed、6 xfailed、1 failed：新增合成测试写了完整 archive 目录名，被既有 isolation 守卫正确拒绝；拆分测试字面量后 focused 与全量均通过。系统 Python 没有 `dotenv` CLI，实际使用 backend venv。Frontend unit/build/E2E、真实 provider、OS credential backend、容量及 Ubuntu/macOS 均为 `not run`；本阶段没有 production route、migration、runtime factory、frontend 依赖或外部请求。提交范围仅为本文件与两份 M0 测试，提交 SHA 以本阶段提交的 HEAD 为准。

## Milestone 1：当前唯一 V1 类型、ports 与 wire contract

### 工作

- [x] 在 `domain/settings_admin` 或更少但等价的模块中定义严格 typed `MutableDefaults`、`SettingsSnapshot`、safe catalog、`CredentialName/Status/Lease`、unit policy/version/override、Admin action/preview/operation 和 tombstone identity；拒绝未知字段/枚举/无界 JSON。
- [x] 公开 DTO 与 secret-bearing `StoredCredential/CredentialLease` 完全分离；secret-bearing type不得实现公开 serialization，masked projection必须由 application显式构造。
- [x] 新增最小 `SettingsStorePort`、`AdminOperationStorePort`，并收敛现有 `KeyStorePort` 的 typed envelope/availability语义；ports不泄漏 ORM、keyring、FastAPI或 provider response。
- [x] 定义稳定 settings revision、unit registry/version identity、admin action/fingerprint/confirmation digest、operation state与 safe result/error；所有状态转换有封闭枚举和不变量。
- [x] 扩展当前唯一 `AnalysisConfigurationSnapshot`，加入 `unit_policy_version` 与 `unit_registry_digest`；旧持久化记录缺字段时只允许 explicit legacy/unknown public projection，不回填当前值。
- [x] 定义 Settings/Keys/Admin application exceptions 与 stable error codes/status mapping，包括 revision、credential allowlist/store、preview、dependency、operation、generation、tombstone 410；继续复用现有 error envelope/validation shape。
- [x] 将 [M9 API route 总表](m9_settings_admin_api.md#2-route-总表) 转成 OpenAPI contract tests与严格 request/response schemas；route尚未实现时可保持预期 red，不得注册返回假成功的 stub。
- [x] 定义 OpenAPI → frontend type generation输入，包含 session SSE union缺口的解决方式；不得把 handwritten duplicate interfaces或 `any` 作为临时完成条件。
- [x] 原子更新 exports、builders、fixtures、history/read-model/schema tests中受 snapshot/domain变化影响的消费者，不保留旧/new snapshot兼容分支。

### Tests

- [x] domain正反矩阵覆盖 settings enum/catalog/model role、credential allowlist、unit entry/digest、Admin state transition、confirmation expiry/action binding和未知字段。
- [x] secret safety tests证明公开 DTO、`repr`、exception、JSON schema和 fixture均不含 credential value或可逆 secret fragment。
- [x] snapshot round-trip覆盖新 M9 record与旧 record legacy/unknown，timezone/unit policy identity不被当前 settings替换。
- [x] ports/architecture tests锁定 domain/application依赖方向、route schema与infrastructure隔离、无 V2/read union/feature flag/第二套 admin operation。
- [x] OpenAPI target tests锁定 route/method/status/header/error code与 key/admin allowlists，未实现行为保持明确 red。

### Verify

- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/domain backend/tests/ports backend/tests/application/test_run_models.py backend/tests/api/test_analysis_schemas.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture -q`
- [x] 运行新增 M9 contract tests并记录 expected red与已转绿范围。
- [x] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [x] `git diff --check`

阶段出口：无数据库/网络/浏览器时即可严格表达 M9 settings、credential、unit、Admin、tombstone与扩展 configuration；所有当前 V1 consumer能编译，公开面无法携带 secret，但尚无 migration、真实 route或运行时行为。

提交边界：domain/application types、ports、exceptions、schemas、exports、fixtures、contract/architecture tests；不得包含 migration、SQL、keyring、provider factory、Admin task或 frontend依赖。

完成记录（2026-10-08）：在 codex/m9-milestone-0（基于 origin/main 的 cf7eed7）完成单一 V1 的 M9 Settings/Keys/Admin/units/tombstone 类型、typed ports、safe error mapping 与 OpenAPI 目标 schema/SSE type source；历史缺失单位字段固定投影为 legacy/unknown，当前生产 route 未添加占位实现。新增领域、端口、架构、HTTP 与预期 red 合同测试，同步受影响的 snapshot/fixture/consumer。

验证：最终 focused 529 passed、architecture 159 passed、M9 API contract 10 passed/26 strict xfailed；此前完整专用 PostgreSQL wateragents_test 回归 2286 passed/26 xfailed（其后仅收紧 M9 DTO 的 strict bool 与新增测试，已由最终 focused/architecture/API contract 覆盖），compileall 和 git diff --check 通过。无 migration/SQL/frontend 依赖；现有 migration head 20261007_01 未改；无真实 provider、OS credential backend、browser、Ubuntu/macOS 或容量验证。26 项 xfail 对应 M3–M8 待实现的真实 route/410/static 能力；本地提交 SHA 以该阶段提交 HEAD 为准。

## Milestone 2：PostgreSQL migration 与 repositories

### 工作

- [x] 在当前 M8 Alembic head之后新增一个可审查 revision；不得修改或重写已发布 migration。
- [x] 创建 singleton `runtime_settings` typed columns/check/FK/revision约束；缺 row时由 service使用 deployment initial defaults，不把完整 `AppSettings` dump写入 JSON。
- [x] 创建 append-only `unit_policy_versions` 与 `unit_policy_entries`，包含 registry digest、parameter identity、target unit和 RESTRICT约束；以当前 registry事实建立 version 1并验证 digest/entries一致。
- [x] 创建 `analysis_run_idempotency_tombstones`，只保存 key/request fingerprint/deleted run id/time；不保存 body/query/receipt/result。
- [x] 创建 `admin_operations`，包含 kind/state/resource/request fingerprint/idempotency key/unique confirmation token digest/bounded safe summary/progress/result/error/timestamps；不创建 confirmation table或 worker-owner/multi-worker列。
- [x] 实现 settings repository singleton read/CAS full replacement、unit version append/read、operation insert/read/list/CAS/progress/terminal/reconcile primitives和 tombstone lookup/write。
- [x] 对同 resource/kind的 pending/running建立数据库约束或等价原子 conflict；同 token digest、同 idempotency key exact-once，不依赖进程内先查后写。
- [x] 扩展 history cleanup store transaction，使 run facts删除、consumption ownership与 idempotency tombstone能在同一事务提交/回滚；此阶段可先实现 repository primitive，不公开 route。
- [x] 更新 schema contract、records/mappers、migration CLI/check、测试清理顺序与 Docker指南；专用 test DB与业务库严格隔离。

### Tests

- [x] fresh database、现有 M8 database upgrade、schema check和受支持 downgrade/upgrade round-trip通过；旧 transcript/RAG事实与 active generation不被重写。
- [x] DB integration直接断言 singleton/check/FK/unique/partial-active约束、unit append-only和 bounded typed JSON validation；非法 row不能只靠 application阻止。
- [x] 两 transaction settings CAS竞争恰好一方成功；stale revision不丢更新，read顺序/identity稳定。
- [x] operation并发创建、同/不同 request fingerprint、token reuse、state CAS、restart query和 terminal immutability有测试，不使用真实 sleep证明竞态。
- [x] history delete+tombstone在注入任一步失败时整体回滚；提交后 live idempotency row与 tombstone互斥，无 dangling consumption link。
- [x] migration bootstrap的 unit version 1与当前 registry digest/entries一致；registry不匹配时 startup fail closed而非静默重建历史version。

### Verify

- [x] `docker compose config`
- [x] 使用专用 `wateragents_test` 运行 `backend/.venv/Scripts/wateragents-db.exe upgrade` 与 `backend/.venv/Scripts/wateragents-db.exe check`
- [x] 使用专用测试数据库运行新增 M9 migration/settings/unit/admin/tombstone persistence tests并记录 revision、数据库名与 basetemp。
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/persistence backend/tests/architecture -q`
- [x] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [x] `git diff --check`

阶段出口：现有 M8 database可安全升级，四类 M9持久化能力与并发/原子约束可复现；尚未公开 settings/key/admin route，也未改变 provider runtime或实现 frontend。

提交边界：一个 migration、schema/records/repositories、依赖锁如确有需要、database tests与指南；不得包含 keyring、provider HTTP、public route、Admin runner或 frontend。

完成记录（2026-10-08）：在 `codex/m9-milestone-0` 新增 M9 revision `20261008_01`、typed singleton settings、封存后不可变的 unit policy、Admin operation与 idempotency tombstone；实现对应 repository，history cleanup在原有 consumption/run 删除事务末尾写 tombstone。增加显式 `--test-database` CLI 守卫，拒绝测试库名缺失或与业务库相同；Docker指南在原文更新。M3 后续 service负责缺失 settings row 的 deployment defaults，本阶段保持 row absent。

验证：只读核对业务库 `wateragents` 与测试库 `wateragents_test` 不同；使用 CLI `upgrade --test-database` / `check --test-database` 验证专用测试库 head `20261008_01`，并在测试库临时 schema 内验证最终 migration 的 M8 downgrade/CLI upgrade/check round-trip、旧 transcript 与 active generation保持。新增 migration/settings/admin/tombstone focused tests 通过；持久化+架构回归 261 passed（`--basetemp=backend/target/m9-m2-persistence-arch-20261008-c`）；`docker compose config -q`、compileall、`git diff --check` 通过。无公开 route、provider调用或 frontend 变更；真实 provider、跨平台与 M9 最终验收未执行。本地提交见该阶段 HEAD。

## Milestone 3：Settings、创建快照与浏览器写保护

### 工作

- [x] 实现 `SettingsService`：validated deployment initial defaults、safe role-filtered model/profile/timezone/memory/resolution catalog、singleton snapshot与 revision CAS full replacement。
- [x] 普通 Settings PUT只允许 timezone/parser model/waterbody model/memory mode/resolution/desired embedding profile；active unit policy只读展示，不能混入普通 replacement。
- [x] desired embedding profile保存只更新期望 identity，不在同一 request内调用 provider、启动 generation或切换 active。
- [x] 实现 `GET /api/settings` 与 `PUT /api/settings` 的严格 DTO/status/error/no-store/request-id；`409 settings_revision_conflict`返回安全 current settings extension并保留前端可重试语义。
- [x] 建立统一 local mutation guard：loopback Host/bind假设、strict `application/json`、Origin same-origin、`Sec-Fetch-Site: cross-site`拒绝、无宽松 CORS、无 GET/form/query mutation；明确无 Origin的本地 CLI分支。
- [x] 将 `RuntimeSettingsResolver` 接入 create boundary：request显式 model/memory/resolution继续由当前 catalog校验；server固化当前 analysis timezone、active unit version/digest，且在 run/idempotency receipt提交前完成。
- [x] 保持 model/memory/resolution在 create body显式提交；settings仅用于 frontend预填，不让 route静默用当前值覆盖已发送 request。
- [x] 原子更新 writer/PostgreSQL/history/detail/SSE/smoke fixtures，使新 run保存完整 configuration，旧 run明确 legacy/unknown；history显示原 IANA identity而非 browser/current setting。
- [x] settings变更与并发 create使用可控 gate证明：变更前接受的 run保留旧 snapshot，变更后新 run使用新 timezone/unit identity，不产生半旧半新配置。
- [x] production composition只创建一个 SettingsService/repository/resolver；route不直接读取 env、SQL或 model config file。

### Tests

- [x] GET/PUT覆盖 revision 0 deployment defaults、first insert、full replacement、stale revision、unknown field、invalid timezone/model/role/profile/enum和 DB unavailable。
- [x] mutation guard覆盖 same-origin browser、cross-site、Origin mismatch、bad/missing content type、form/query/GET、loopback CLI与 proxy host边界；拒绝发生在 service side effect之前。
- [x] create snapshot测试覆盖 settings race、explicit create fields、timezone/unit identity、legacy read、DST和原 IANA展示；当前 settings变化不改写旧 history。
- [x] desired profile保存断言 `0 provider call / 0 generation build / 0 active switch`，只改变future desired projection。
- [x] route/error/log capture证明 deployment config与 secret不进入 Settings response、conflict extension、validation、日志或 OpenAPI example。
- [x] M1–M8 create/history/SSE/idempotency回归保持，settings route不能新增第三 stream或改变现有 route名称。

### Verify

- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/analysis_runs backend/tests/application/transcripts backend/tests/api/test_analysis_runs.py backend/tests/api/test_analysis_history.py -q`
- [x] 运行新增 settings service/API/mutation-guard tests与 snapshot persistence tests。
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/api/test_openapi_contract.py backend/tests/architecture -q`
- [x] 使用专用 PostgreSQL运行 settings CAS/create/history/restart focused integration。
- [x] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [x] `git diff --check`

阶段出口：Settings API、same-origin mutation policy与future-run configuration materialization通过真实 production path；历史保留原timezone/unit identity，尚未动态替换 provider credentials或公开 Admin动作。

提交边界：SettingsService/route/schema、mutation guard、create resolver与 snapshot/history消费者、focused tests；不得包含 OS keyring、per-run provider重组、unit intent、Admin runner或 frontend。

完成记录（2026-10-08）：`codex/m9-milestone-0` 基于 `origin/main` 的 `cf7eed7` 完成 SettingsService、safe catalog、`GET/PUT /api/settings`、revision CAS/current conflict extension、local mutation guard 与 create 前的 RuntimeSettingsResolver。新 run 保存原 IANA timezone 和 active unit version/digest；旧 run 的 legacy/unknown 保持可读，SSE 沿用现有无 configuration 的安全投影。多 profile 目录仅扩展可选的 desired identity，保存不触发 provider/generation/active 切换；M4 再处理 provider lease 与凭据。

验证：分析/历史 153 passed，OpenAPI/架构 160 passed；扩展 API/application/domain/infrastructure 1919 passed、1 skipped（未配置专用 DB 时跳过 PostgreSQL 用例）、24 xfailed（后续 M9 route 预期红）；最后的 Content-Type 边界改动后 Settings/create/SSE 126 passed。专用 `wateragents_test` 隔离 schema 中 settings CAS、并发前后 run、legacy history 与应用重启 1 passed；compileall 与 `git diff --check` 通过。secret 未进入 Settings 响应、409 extension、验证响应、日志与 OpenAPI；真实 provider、OS keychain、跨平台及 M9 整体验收未运行。本地提交见该阶段 HEAD。

## Milestone 4：OS credentials 与 per-run/attempt provider runtime

### 工作

- [x] 添加并锁定受支持的跨平台 OS credential library；记录实际解析版本与 Windows Credential Manager/macOS Keychain/Linux Secret Service backend要求，不承诺未验证backend。
- [x] 实现 namespaced `OSKeyStore` adapter，只接受 `openai/google_geocoding` typed account和 versioned stored envelope；startup/operation识别 secure backend、unavailable与 insecure/plaintext fallback并 fail safe。
- [x] effective credential resolver先读 user store，缺 override再读 startup deployment `SecretStr`；masked status只给 source/configured/hint/updated/store availability，不回显或持久化 value。
- [x] 实现 `GET /api/keys`、`PUT/DELETE /api/keys/{key_name}` 与显式 `POST .../test`；save不联网，delete只删 user override，deployment fallback仍显示 configured，test不自动retry且只返回 safe result/error。
- [x] 将 private value注册到现有 redaction boundary；route/service/adapter/provider异常在进入 logger/public error前去除 secret、Authorization、OS path/backend internals和 raw body。
- [x] 将共享 HTTP pool改为不带固定 Authorization/default secret header；实现 `RunProviderBundleFactory`，从 run configuration与 immutable leases构造 parser、geocoding、waterbody/run-summary、session query-embedding adapters。
- [x] analysis workflow/coordinator通过明确 factory/per-run execution context取得 bundle；不得用 ContextVar、全局 mutable adapter field或每次 tool call重读 settings/key。
- [x] bundle随 run lifecycle释放；replace/delete key不影响已创建 run，新 run使用新 effective key。cancel/shutdown不泄漏lease，也不错误关闭全局 HTTP/DB资源。
- [x] M8 background embedding worker在每个 claimed attempt开始取得一次 lease，attempt内不切换；下一 attempt才读取新key。missing/unavailable按既有 safe retry/failure语义处理，不改run terminal。
- [x] production/test/smoke builders走同一个 factory与lease contract；tests只注入 in-memory fake store/provider，绝不访问开发者真实 OS keychain。

### Tests

- [x] fake key store contract覆盖get/set/delete/envelope version/atomic replace/availability/error/mask/allowlist；真实 keychain不作为默认测试前置。
- [x] key API覆盖 user/deployment/none source、save无网络、delete fallback、missing/store unavailable、explicit test success/safe failure/transport unavailable和unknown key。
- [x] secret scan/capture覆盖 public JSON/OpenAPI/repr/log/traceback/metrics/DOM-bound fixtures；完整/部分secret均不出现，masked hint不可逆且不冒充验证成功。
- [x] two-run controlled gate证明旧run持旧lease、新run用新key，replace/delete不切换在途request；同一run parser/geocoding/waterbody/summary/semantic query使用预期lease。
- [x] M8 worker attempt测试覆盖key在attempt中替换、下一attempt读取、missing key、cancel、retry和shutdown；provider failure不改变原run状态。
- [x] production composition test证明一个共享无secret HTTP pool、per-run adapters和一个M8 worker；没有startup长期captured secret路径或隐藏legacy branch。

### Verify

- [x] 运行新增 credential domain/service/OS-adapter/key API tests，明确全部使用fake backend。
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/infrastructure/test_m4_runtime.py backend/tests/infrastructure/test_m6_runtime.py backend/tests/infrastructure/test_m8_runtime.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/analysis_runs backend/tests/application/transcript_memory backend/tests/api/test_openapi_contract.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture -q`
- [x] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [x] `git diff --check`

阶段出口：OpenAI/Google key可通过安全OS store管理，公开面只有masked状态；所有可变secret沿per-run或per-attempt lease进入真实provider path，旧run不受后续修改影响，且没有默认测试访问真实keychain/网络。

提交边界：dependency lock、OS adapter、CredentialService/API、redaction、provider/runtime factory与focused regressions；不得包含真实credential、真实provider artifact、unit intent、Admin cleanup或 frontend。

完成记录（2026-10-08）：锁定并安装 `keyring==25.7.0`；生产 `OSKeyStore` 仅接受 Windows `WinVaultKeyring`、macOS `Keyring`、Linux `SecretService.Keyring` 且 priority 为正的 backend，其他 backend/unavailable 关闭 user-store 操作；三个平台的真实系统 keychain 均未作为测试前置，也未声称跨平台实机通过。`CredentialService` 提供 user→deployment precedence、masked status 与显式一次测试，OS envelope 固定 service/typed account/schema version。生产启动不再用 startup OpenAI/Google key 建长期 adapter；共享无默认 secret header 的 provider pool 由 run factory 以一次性 lease 构造 M4/M6/M7/查询 embedding，M8 index worker 每个 claimed batch attempt 重新 lease。创建失败、完成、取消和 shutdown 释放 bundle；公开响应、日志、traceback 与 OpenAPI 只保留安全投影。

验证：fake key/API/run/attempt 新用例 10 passed；M4/M6/M8 runtime 扩展 58 passed，analysis/transcript-memory/OpenAPI 191 passed，architecture 159 passed；真实 loopback Host/Origin 与 SSE smoke 9 passed；专用 `wateragents_test` 隔离 PostgreSQL 完整后端回归 2320 passed、19 xfailed（后续 M9 预期红）；compileall、`git diff --check` 通过。真实 provider、三个 OS keychain 实机及 M9 整体验收尚未运行。

## Milestone 5：全局单位 policy 与 session override

### 工作

- [x] 实现 `UnitPolicyService` 与 reusable bounded confirmation registry；global policy preview绑定base version、normalized entries、registry digest、fact fingerprint、required phrase与expiry，restart后未执行preview失效。
- [x] 实现 `POST /api/settings/unit-policies/preview` 与 confirm route；执行只创建append-onlyversion并原子更新settings active version，普通Settings PUT不能旁路。
- [x] preview/confirm重新验证ParameterKey、target unit id、alias、dimension、registry digest、base version和current facts；stale/expired/wrong action/wrong phrase/token reuse返回稳定错误且0写入。
- [x] 扩展query-understanding typed output，只允许bounded raw unit alias、可选parameter reference/rationale；parser不能提供formula、unit conversion factor或新增parameter。
- [x] 实现 `UnitPreferenceResolver`：registry normalization、ambiguity/dimension/sealed parameter scope、global fallback与session override优先级；unknown/ambiguous/incompatible产生typed issue。
- [x] 将允许候选加入waterbody reasoning task/skeleton；Agent只判断候选是否适用于参数，不能选择allowlist外unit或修改source value。
- [x] conversion继续复用现有 `convert_evidence_unit`/decimal/quality policy；validator确认target unit、registry digest、parameter/window/evidence closure，拒绝前端或模型自算结果。
- [x] typed decision/result保存session override source与registry digest；history/replay保持原identity，global policy后续变化不改写旧session或source record。
- [x] 更新create snapshot、query/context、M6 result assembler、zero-model/summary、persistence/readers/fixtures的全部消费者；无unit intent时保持当前科学语义。

### Tests

- [x] global policy preview/confirm覆盖valid、base stale、registry drift、unknown/duplicateparameter、unknown/incompatibletarget、token expiry/reuse/action mismatch与transaction rollback。
- [x] query unit intent正反fixture覆盖alias、ambiguous、unknown、parameter-specific、scope外parameter、formula injection、duplicate和bounded size。
- [x] precedence测试覆盖session override > global entry > source unit，无override不改变当前结果；global更新只影响新run。
- [x] reasoning/validator fixture证明Agent只能在候选中判断applicability，conversion由确定性工具完成，dimension/closure不匹配fail closed。
- [x] history/persistence round-trip保留unit policy version/digest与session override source；旧记录显示legacy/unknown，不被current policy补写。
- [x] unit功能不改变M5 target/discharge gates、M8 historical contextual-only权限或source facts/confidence/quality。

### Verify

- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/evidence_policy backend/tests/application/reasoning backend/tests/domain -q`
- [x] 运行新增 unit policy service/API/query-intent/snapshot focused tests。
- [x] 使用专用PostgreSQL运行unit version/confirmation/create/history integration tests。
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/infrastructure/test_m6_runtime.py backend/tests/api/test_openapi_contract.py backend/tests/architecture -q`
- [x] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [x] `git diff --check`

阶段出口：global unit policy只有preview/confirm可append并影响future run，session override在sealed scope内经过deterministic normalization与M6 validator闭环；旧history/source facts保持不变。

提交边界：confirmation registry、unit service/API、query intent/resolver/reasoning/validator、snapshot/history与focused tests；不得包含Admin cleanup/RAG route、frontend或未授权真实provider请求。

完成记录（2026-10-08）：全局单位策略经有界进程内 preview/单次确认、专用 PostgreSQL 回执与同事务 append-only version/settings 指针更新生效；重启后未确认 token 失效，相同 idempotency bytes 可重放。查询解析只保留有界单位别名，resolver 在 sealed 参数范围内确定 session override 优先级；M6 planning/skeleton 限定候选，decimal conversion 和证据闭包验证沿用既有工具与 validator，已验证 decision/result 记录来源及 registry digest。旧 run 配置、history 与 source record 不被当前 policy 改写。

验证：专用 `wateragents_test` PostgreSQL 完整后端回归 2341 passed、16 xfailed（后续 M9 预期红）；M5 确认、回滚、重启、query intent 与 reasoning focused 用例通过，compileall 和 `git diff --check` 通过。未运行真实 provider 请求或跨平台实机验收。

## Milestone 6：Admin operation、history tombstone 与 RAG 运维

### 工作

- [x] 实现 `AdminService`：preview读取current facts、normalized action/dependencies/counts/preserved resources，token digest/action digest/fingerprint/phrase/expiry只放有界进程内registry；token不承载secret或完整selection明文。
- [x] 实现execute顺序：registry lock validation → 重新读取facts/fingerprint → DB transaction insert/return operation → commit后schedule → CAS pending/running → typed handler → terminal safe result/error。
- [x] 实现 `AdminOperationRunner` 单process/worker生命周期；startup重新schedule pending，遗留running按底层durability reconcile或收敛interrupted/failed，不能永久running或引入distributed owner。
- [x] 实现Admin storage/RAG health与operation list/detail routes；列表cursor/limit稳定，GET无side effect，Admin不新增SSE或operation cancel route。
- [x] 实现history cleanup preview/execute：明确run ids或all-history snapshot、M8 dependency graph、consumer-first whole-run cascade、preserved keys/settings/unit versions和bounded counts。
- [x] 执行history delete时复用同一个 `RAGMaintenanceOperations`/store transaction，并原子写idempotency tombstone；不能先删后补、TRUNCATE绕过ownership或留下dangling ref。
- [x] 将create idempotency lookup扩展为live row+tombstone互斥：same raw bytes 410、different bytes 409、新key 201；frontend可用safe error，不返回旧query/result。
- [x] 公开RAG profile handoff、failed-job retry、inactive generation delete preview/execute；严格复用M8 state machine、reader lease/retention/build/validate/activate，不复制repository logic。
- [x] profile desired/active/building/reader-drain/cleanup-ready完整投影；新generation激活前旧adapter/credential仍可用，index-only操作不删除history。
- [x] storage/status明确 `evidence_cache.state=not_present`；不存在任何evidence-cache mutation route、button contract或伪统计。
- [x] operation safe summary/result/error不保存secret、canonical text/vector/provider body/完整cleanup preview；日志/metrics使用bounded labels。

### Tests

- [x] preview/execute矩阵覆盖expiry、wrong phrase/action/key、fact stale、same/different idempotency bytes、token double use、concurrent action和0 side effect rejection。
- [x] operation runner使用可控gate覆盖commit-before-schedule crash、pending restart、running reconcile、handler failure、terminal immutability、HTTP disconnect/UI离开与同resource conflict。
- [x] history cleanup fixture覆盖no consumer、one/transitive consumer、stable conflict、explicit whole-run cascade、concurrent new consumption、rollback/retry、tombstone 410/409与无dangling refs。
- [x] RAG Admin fixture覆盖desired already active、building/failed/retired/active、retry safe codes/limit、reader lease、retention、handoff activation和index-only deletion；history/API keys不变。
- [x] API/OpenAPI测试覆盖所有新增Admin method/status/header/error/cursor/202 operation shapes，并证明没有第三SSE、direct search、arbitrary SQL/filter或evidence-cache mutation。
- [x] restart/production composition证明route、runner与M8 worker共享同一maintenance/repository graph，且只有一个Admin runner。

### Verify

- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/application/transcript_memory/test_maintenance.py backend/tests/persistence/test_rag_cleanup.py backend/tests/persistence/test_rag_consumption_links.py -q`
- [x] 使用专用PostgreSQL运行新增Admin operation/history tombstone/restart tests。
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/api/test_analysis_runs.py backend/tests/api/test_analysis_history.py backend/tests/api/test_openapi_contract.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture backend/tests/infrastructure/test_m8_runtime.py -q`
- [x] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [x] `git diff --check`

阶段出口：所有破坏性动作都经server preview/confirmation与persisted operation；history/tombstone、M8 consumption ownership和generation handoff无悬空/越权，public surface不存在假cache或第三stream。

提交边界：Admin service/runner/routes、history transaction/tombstone/create lookup、M8 maintenance exposure、focused PG/API tests；不得包含frontend、真实provider调用或无关RAG重构。

完成记录（2026-10-08）：Admin preview/confirm 使用有界单用途 token 和当前事实指纹；`20261008_03` migration 持久化有界恢复命令，operation 先提交再调度，pending 重启恢复、遗留 running 收敛为 interrupted。History 清理复用 M8 同事务 consumer-first 删除并写 tombstone；旧创建 key 相同原始请求返回 410、不同请求返回 409。RAG profile handoff、失败 job retry 和 inactive generation 删除复用 M8 state machine；storage 明示 `evidence_cache=not_present`，all-history 单次最多 500 个 run/依赖链接，超限返回 422。

验证：专用 `wateragents_test` PostgreSQL 全量后端回归 2367 passed、1 xfailed；M8 maintenance/cleanup/consumption、analysis/history/OpenAPI、architecture/runtime 及 Admin restart/cascade/disconnect 专项用例通过，compileall 与 `git diff --check` 通过。未运行真实 provider 或跨平台实机验收；M7–M9 和最终整体验收仍待完成。本地提交 SHA 以该阶段提交 HEAD 为准。

## Milestone 7：Frontend foundation、实时分析与 History

### 工作

- [x] 在唯一 `frontend/` 初始化 React + TypeScript + Vite与 committed `package-lock.json`；固定Node/npm支持范围和 `lint/typecheck/test/build/test:e2e/generate:api/check:api` scripts，不读取或复制`frontend_bk/`。
- [x] 仅添加首版所需依赖：React Router、Leaflet/react-leaflet、受控chart library、测试工具和单一SSE parser；不引入WebSocket、Redux/event bus、第二design system或插件框架。
- [x] 从backend OpenAPI生成/校验提交的API types；CI drift check失败时先修backend schema，不用`any`、重复handwritten DTO或unchecked cast绕过。
- [x] 实现统一typed HTTP client：relative `/api`、status/content-type/request-id/error envelope、GET bounded network retry、mutation不自动retry、secret-safe error/log policy。
- [x] 实现单一fetch-SSE parser/client，覆盖UTF-8跨chunk、CRLF/LF、multi-line data、keepalive、id/event/JSON、stream_end、400/409/410；不假设chunk=frame。
- [x] 建立feature reducers：run replacement snapshot、selected session transcript assembly、persistence、UI selection/form draft分别所有；迟到GET不覆盖已收到SSE状态。
- [x] 实现URL与shell：`/`、`/history`、`/runs/{run_id}`、`/runs/{run_id}/waterbodies/{session_id}`、`/admin`占位、not-found；桌面三栏与窄屏tabs/drawers，保留高频入口位置。
- [x] 实现New Analysis表单，字段严格等于current create request；default/catalog来自Settings，UUID v4 key与exact body bytes在同一提交重试中保持，用户新Start才生成新key。
- [x] 实现run stream与按需一个selected session stream：切水体释放旧session连接但保持run连接；首次live不先GET，ended/history用GET，410转真实history URL。
- [x] 实现地图/topology与七种视觉、legend/label、蓝色外圈呼吸、无session pending/cancelled-before-start和选中identity；截图测试冻结动画，不新增reduced-motion设置。
- [x] 实现Overview/Agent/Evidence三tab、message started/delta/finalized原位更新、折叠typed records/full content、current vs historical evidence和persistence retry；不展示hidden reasoning/raw prompt/provider response。
- [x] 实现History cursor分页、run选择/当次地图/session读取、原IANA timezone/legacy unknown；不发明total/filter、合并相同query/COMID或用current data重跑。
- [x] History页面只提供当前单个run的删除入口，并复用server cleanup preview/confirmation；批量/all-history只在Admin，RAG index/generation维护不得混进History删除动作。
- [x] time-series只渲染backend series/window/unit/quality/availability；不补点、自行换单位、生成risk或用confidence公式造band。

### Tests

- [x] package scripts在clean install下可重复；dependency/license/build输出不含archive path、secret或未追踪generated source。
- [x] HTTP client测试覆盖create exact bytes/key重试、mutation no-retry、content-type/error/request-id、410 tombstone与secret-safe logging。
- [x] SSE parser golden/property fixture覆盖chunk切分、UTF-8、CRLF、multi-line、keepalive、duplicate id、different delta same item、malformed frame、EOF/stream_end和400/409/410。
- [x] reducer/integration测试覆盖GET/SSE race、run replacement、session cursor dedupe、message原位finalize、switch abort、run terminal/persistence独立和close-view不cancel。
- [x] component/map测试覆盖七种视觉与文字legend、running动画冻结、无session节点、three tabs、historical contextual-only、原timezone和persistence retry。
- [x] History测试覆盖opaque cursor、stable order、invalid cursor error、run/session identity、strict saved facts、legacy timezone、single-run删除，以及页面不存在批量/index/credential cleanup入口。
- [x] architecture/source scan证明`frontend/`没有`frontend_bk` import/copy、WebSocket、third SSE、secret storage、arbitrary provider/base URL或single-agent cancel。

### Verify

- [x] `npm --prefix frontend ci`
- [x] `npm --prefix frontend run lint`
- [x] `npm --prefix frontend run typecheck`
- [x] `npm --prefix frontend run test -- --run`
- [x] `npm --prefix frontend run check:api`
- [x] `npm --prefix frontend run build`
- [x] backend OpenAPI/architecture/current analysis/history/SSE focused regressions通过。
- [x] `git diff --check`

阶段出口：用户可以通过新frontend创建/取消/查看live run与一个selected Agent、阅读地图/三tab/history/persistence；typed client与两层SSE行为可复现，Settings/Admin完整功能留给下一阶段。

提交边界：frontend toolchain/lock、generated types、HTTP/SSE、reducers、shell、analysis/map/detail/history及测试；backend只允许为真实schema export修正契约，不夹带Settings/Admin UI或static hosting。

完成记录（2026-10-08）：唯一 `frontend/` 使用 React/TypeScript/Vite、当前后端 OpenAPI 生成类型、统一相对路径 HTTP/fetch-SSE client 和 feature reducers；提供创建/取消分析、run 与单个 selected session 实时流、七态地图、Overview/Agent/Evidence、后端 time-series、History cursor 与单 run 删除 preview/confirmation。后端仅补充 `HydrologyGraph`、`InferenceResult`、`RunSummaryResult` 的 public schema 导出；前端生产源码扫描禁止 archive、WebSocket、第三 SSE、secret storage 和单 Agent cancel。Settings/Admin 完整 UI 与后端静态托管归 Milestone 8。

验证：`npm ci` clean install、lint、typecheck、21 项单元/组件测试、OpenAPI drift check、build 与 bundle scan 通过；Chrome deterministic fake browser 3 项通过，覆盖创建→run SSE→selected Agent SSE→取消→History、窄屏面板及冻结动画的七态地图截图。后端 OpenAPI/analysis/history/SSE/architecture 聚焦回归 359 passed；`git diff --check` 通过。未运行真实 provider 或跨平台浏览器验收；M8–M9 和 M1–M9 整体验收仍待完成。本地提交 SHA 以该阶段提交 HEAD 为准。

## Milestone 8：Frontend Settings/Admin 与 production hosting

### 工作

- [x] 实现Settings drawer：analysis defaults、Units、Memory/RAG、Credentials和Admin入口；关闭保留当前run视图，未保存draft确认丢弃。
- [x] 普通settings full replacement携带revision；409保留draft并展示server current values，要求用户重新确认，不做field-level自动merge。
- [x] 实现global unit preview/confirm UI，展示diff/version/registry与required phrase；不能通过普通save或本地checkbox绕过新preview。
- [x] 实现OpenAI/Google credential rows与save/delete/test；输入提交后从component state/DOM清除，deployment fallback语义正确，USGS/Grok/arbitrary provider不出现。
- [x] 实现Admin storage/RAG/operations页：history cleanup、profile handoff、failed jobs retry、inactive generation delete、active/recent operations；不提供clear-all/evidence-cache/arbitrary SQL/generation文本执行。
- [x] Admin preview展示dependencies/cascade/counts/preserved resources/tombstones/reader/usage warning；execute只回传server token/phrase与新UUIDv4 idempotency key，stale/expired重新preview。
- [x] operation收到202后只在Admin可见且active时按server hint有界poll；离开停止poll但不取消，返回按identity恢复，terminal停止，不能创建第三SSE。
- [x] 配置Vite dev proxy代理`/api`和SSE并保持streaming bytes；业务代码继续只用relative URL。
- [x] backend static responder同origin提供`frontend/dist`：API/SSE route优先、HTML navigation fallback、API 404保留JSON、MIME、hashed immutable/index no-cache、HEAD/path traversal/dist missing有界处理。
- [x] production composition/health/startup log安全表达frontend available/unavailable；不得fallback到`frontend_bk/`或因dist missing阻止纯API tests/CLI构造app。
- [x] 增加desktop/narrow screenshot baselines，以M9自身为准；不要求旧UI逐像素一致，不移动多个已冻结高频入口。

### Tests

- [x] Settings component/integration覆盖defaults/catalog、revision conflict/draft、invalid field、server unavailable、unit preview/confirm和future-run提示。
- [x] credential UI测试证明raw value不进DOM snapshot/console/localStorage/error，save不触发test，delete deployment fallback与store unavailable准确。
- [x] Admin integration覆盖preview/phrase/token/idempotency、cascade second preview、202/poll/reopen/terminal、stale/expired/conflict与无evidence-cache按钮。
- [x] static hosting测试覆盖index/assets/MIME/cache/HEAD/SPA fallback/API 404/SSE headers/path traversal/dist missing；frontend_bk永不读取。
- [x] dev proxy integration证明SSE跨chunk实时到达、不buffer/改写，Origin/Host行为满足backend local mutation guard。
- [x] visual tests覆盖桌面三栏、窄屏降级、七种地图状态、Settings drawer、Admin operation；动画/时间/随机identity在截图中受控。
- [x] production browser smoke从backend-served build访问相对API，无CORS、绝对dev URL、source map secret或archive asset。

### Verify

- [x] `npm --prefix frontend ci`
- [x] `npm --prefix frontend run lint`
- [x] `npm --prefix frontend run typecheck`
- [x] `npm --prefix frontend run test -- --run`
- [x] `npm --prefix frontend run build`
- [x] `npm --prefix frontend run test:e2e`（使用deterministic fake backend，不产生外部调用）
- [x] 运行新增backend static-host/settings/key/admin API integration tests与OpenAPI tests。
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture backend/tests/api backend/tests/infrastructure -q`
- [x] 对source、bundle、snapshot、console与test artifacts执行secret/archive-path scan。
- [x] `git diff --check`

阶段出口：Settings/Keys/Admin与production-hosted frontend通过同origin真实surface工作；用户离开/重开页面可恢复operation，secret与archive不进入browser/build，API/SSE语义未被SPA破坏。

提交边界：Settings/Admin UI、proxy/static hosting、browser integration/visual tests与必要backend wiring；不得包含真实provider结果、installer/desktop/auth/remote部署或无关UI重设计。

完成记录（2026-10-08）：Settings drawer 接入当前 catalog、revision/full replacement、154 项 registry 单位选项与独立 preview/confirm；密钥只呈现 OpenAI/Google 允许动作，保存清空输入且测试连接须单独触发。Admin 提供 storage/RAG/history cleanup、profile handoff、失败 job retry、inactive generation 删除和 operation 恢复；同类活动 operation 不重复提交。后端按同源提供生产构建，API/SSE 优先，health header 与 startup log 表达 frontend 可用性；Vite 代理保留 SSE 分块和本地 Origin/Host 语义。当前 operation API 未提供 poll hint，Admin 使用立即/1/2/5 秒有界轮询并在页面隐藏或终态停止。新增 Settings、Admin、静态托管、代理和浏览器截图回归，未创建额外说明文档。

验证：`npm ci`、lint、typecheck、OpenAPI drift check、build 与 33 项 frontend 测试通过；Chrome deterministic E2E 4 passed、1 production-only skipped，另以 backend-served build 跑 production Chrome smoke 1 passed。新增及既有 backend architecture/API/infrastructure 使用 `wateragents_test`、`--basetemp=backend/target/m9-m8-broad-pg-final-20261008` 为 726 passed；最后静态缓存调整后 focused API 82 passed。源码、bundle、截图与 test artifacts 共 29 文件扫描无 secret/archive 命中，浏览器 console 断言无提交的假密钥，`git diff --check` 通过。构建报告现有单 JS chunk 约 825 kB 警告；真实 provider、OS credential backend、跨平台浏览器与 M9 整体验收尚未执行。本地提交见该阶段 HEAD。

## Milestone 9：端到端验收、契约审计与交付证据

### 工作

- [x] 新增M9 deterministic fake E2E runner/test，只通过production app factory、production routes、backend-served frontend与真实PostgreSQL边界；fake只替换外部provider/OS keyring/时钟，不开放test-only production switch/header/query。
- [x] E2E覆盖settings/key save无网络 → create future run → run SSE/topology → selected session SSE → result/evidence/summary → history/original timezone → persistence retry语义。
- [x] 同一suite覆盖cancel：only run cancel、running session真实收敛、未开始节点灰色虚线、真实cancelled橙色、已完成/failed/cancelled不被run状态重染。
- [x] E2E覆盖settings/key替换：在途run保留旧snapshot/lease，之后run使用新值；test connection与real analysis分别由明确动作触发，fixture证明无隐式provider call。
- [x] E2E覆盖unit global version与session override、history round-trip、legacy/unknown和M6 validator；科学gate/confidence/evidence closure不被前端或单位偏好改写。
- [x] E2E覆盖history cleanup dependency conflict/whole-run cascade/tombstone 410/409、RAG profile handoff/reader drain/retry/delete、operation restart/polling与keys/settings/unit preservation。
- [x] PostgreSQL restart测试从现有M8 head upgrade，跨至少一次process restart验证settings/unit/admin/tombstone/history/RAG/readers/static frontend；不得使用业务数据库。
- [x] 从create入口沿settings resolver、credential lease、M4/M5/M6/M7/M8、writer/history/SSE/Admin/static frontend做contract sweep，搜索startup captured secret、旧snapshot、第二maintenance owner、archive/WebSocket/third SSE/fake cache与duplicate DTO。
- [x] 运行M1–M9 focused、完整non-PG、专用PostgreSQL、architecture/OpenAPI、frontend unit/build/browser E2E、smoke、compileall、docs/link/fence/contract/secret audits；逐项记录真实数字。
- [x] 只有用户明确授权、credential preflight成功且接受可能计费时，才运行最小OpenAI/Google真实smoke；USGS key不进入UI smoke。未授权/未配置一律记录`not run`。
- [ ] 在Ubuntu/macOS/Windows对同一commit运行支持的backend/frontend/build/browser gates；缺平台证据保持`partial`，不把Windows本机结果外推。
- [x] 填写本文件全部实际完成项、阶段记录与contract matrix，同步M9五份设计、root architecture/milestones/deferred/frontend docs、README/startup说明和OpenAPI；代码未实现项保持unchecked/pending。
- [x] 审查staged/unstaged/untracked/permission-limited范围、migration head、dependency locks、generated types与artifacts；不提交secret、OS credential dump、database volume、`frontend/dist`（除非仓库明确要求）、Playwright report、pytest cache或无关文件。

### Tests

- [x] fake browser E2E对真实业务facts、identity、status、SSE sequence、map state、typed transcript、history timezone、settings revision、operation result和cleanup ownership做断言，不只断言页面出现/HTTP 200。
- [x] fault E2E覆盖DB/key store/provider/network/SSE interruption、invalid/stale token、process restart和dist missing；每种失败明确哪些side effect不存在、用户看到什么safe状态和能否retry。
- [x] PG E2E证明migration、CAS、unit append-only、operation/tombstone原子性、history/RAG cleanup与restart，不扫描或修改业务库。
- [x] frontend/browser证据覆盖desktop/narrow、keyboard focus、七种地图状态、two-stream connection count、secret absence、API 404/static cache与no archive asset。
- [x] contract matrix每项含实际production位置、具体test和docs status；provider/platform/capacity为partial/not run时不标implemented。
- [x] docs audit证明链接/fence/checkbox/contract ID/stage exit/命令无破损，本文件没有因文档存在而误勾选implementation项。

### Verify

- [x] 运行新增M9 backend/fake/PG smoke与production-entrypoint runner，记录实际命令、数据库、basetemp和结果。
- [x] `backend/.venv/Scripts/python.exe -B -m pytest backend/tests/architecture backend/tests/application backend/tests/domain backend/tests/ports backend/tests/infrastructure backend/tests/api backend/tests/smoke -q`
- [x] 使用专用`wateragents_test`运行`backend/.venv/Scripts/python.exe -B -m pytest backend/tests -q`
- [x] `backend/.venv/Scripts/python.exe -B -m compileall -q backend/app backend/scripts`
- [x] `npm --prefix frontend ci`
- [x] `npm --prefix frontend run lint`
- [x] `npm --prefix frontend run typecheck`
- [x] `npm --prefix frontend run test -- --run`
- [x] `npm --prefix frontend run check:api`
- [x] `npm --prefix frontend run build`
- [x] `npm --prefix frontend run test:e2e`
- [x] 执行docs link/fence/contract-ID/unchecked-count、OpenAPI drift、secret、archive import/path和build artifact audits并保存实际输出。
- [x] `git diff --check`
- [x] `git status --short --branch`，并核对local HEAD、upstream与remote SHA。

阶段出口：M9 backend、frontend、PostgreSQL、OS-store abstraction、provider lease、units、Admin、history/SSE/static hosting在production入口有可复现的fake/PG/browser证据；所有适用契约有真实承接，未运行的外部边界如实记录，M1–M9第一版可以进入最终交付评审。

提交边界：E2E/smoke、审计修复、completion evidence、contract/root docs与启动说明；不夹带未设计功能、真实secret/provider artifact或无关重构。最终提交前列出相对`origin/main`的完整文件范围与commit SHA，未经用户要求不push。

### M9 完成记录（2026-10-08）

- 分支 `codex/m9-milestone-0`；M9 前 HEAD `67972093a2608c42054d3447d4cff1c831c0304e`，merge-base / 本地及远端 `origin/main` 均为 `cf7eed70aa43fd6b4a2303a7e0b933804a113735`。M9 无新 migration；当前唯一 head `20261008_03`。`frontend/package-lock.json` 与生成 API types 无变更，`check:api` 通过。
- 新增 `backend/tests/smoke/test_m9_production_pg.py`，只替换外部 provider、OS key store 与时钟，通过 production `create_app`、HTTP/SSE、backend-served build 和隔离的 `wateragents_test` schema 完成 run、cancel、在途 lease、后续配置、证据/结果、历史、重启、Admin cleanup/operation 与 tombstone 断言；`frontend/e2e/production.spec.ts` 对真实保存事实、两层 SSE、地图、原时区、operation/cleanup 及重启后 410 历史转接做 Chrome 断言。单位/M6 validator、RAG handoff/reader/cleanup 与故障矩阵由同一完整回归中的 PG API、application、frontend browser/unit 用例分层覆盖，并非每个故障都在同一 Playwright 用例内重演。
- 发现并修复 M5 合成 evidence fact 的非 UUID operation identity，保证 production writer 可保存缺失事实；日志回归只比较故障注入之后的记录；M8 PostgreSQL 专属 smoke 在无测试库时明确 skip。M9 fake clock 曾比数据库实时时钟超前一天，使 RAG job 触发 `updated_at >= created_at` 约束；改为每个测试 app 创建时固定当前 UTC，重跑无 reconciliation 告警。没有改动业务 migration、锁文件或生成类型。
- 专用 PostgreSQL：`WATERAGENTS_TEST_DB_NAME=wateragents_test` 与 `python -m dotenv -f backend/.env run --no-override --`，`pytest backend/tests -q --basetemp=backend/target/m9-m9-full-pg-elevated-final-20261008` 为 **2372 passed、2 第三方 deprecation warnings**；时钟修正后 M9 production PG/Chrome `pytest backend/tests/smoke/test_m9_production_pg.py -q -s --basetemp=backend/target/m9-m9-production-browser-clock-final-20261008` 为 **2 passed、无 RAG reconciliation 告警**。现有 M8 head upgrade、fresh schema、CAS、append-only、Admin 原子性、restart 在 PG tests 中验证，未访问业务数据库。
- 无测试库的指定 architecture/application/domain/ports/infrastructure/api/smoke 集合为 **2190 passed、12 skipped、2 第三方 warnings**（`--basetemp=backend/target/m9-m9-nonpg-subset-elevated-final-20261008`）；直接无测试库执行整个 `backend/tests` 会把 PostgreSQL 专属 persistence tests 收进来并因缺少 `WATERAGENTS_TEST_DB_NAME` 失败，不属于 non-PG 门禁。此前完整 PG 首轮 2371 passed；新增取消场景后以上 2372 为最终后端结果。
- `npm --prefix frontend ci`、lint、typecheck、`test -- --run` **33 passed**、`check:api`、build 均通过；`WATERAGENTS_BROWSER_CHANNEL=chrome npm --prefix frontend run test:e2e` **4 passed、1 production-only skipped**，该 production-only 用例另在 PG/Chrome runner 中通过。build 仍提示约 825 kB 的单 JS chunk 警告。M4/M5/M6/M7/M8 离线 smoke、health、history restart 与 154/154 capability audit 通过；`compileall`、`git diff --check` 通过。
- 2026-10-09 审查修复：前端按 `topology_ready.content_ref` 读取并校验大于内联阈值的拓扑，切换 run/view 时取消旧请求；地图选点、COMID、手填坐标可相互切换并清除地图点。新增 live/history 引用拓扑 Chrome E2E 与目标切换组件回归；本轮 frontend 测试 **35 passed**、Chrome fake E2E **5 passed、1 production-only skipped**，lint、typecheck、build 通过。未改后端，本轮未重跑 PostgreSQL 或 production-only 浏览器用例。
- 2026-10-09 审查问题修复：Admin runner 对数据库暂不可用的状态写入在同一进程内重试，并核对可能已提交的状态，业务操作不重复执行；历史过程正文沿页面 `history` 视图读取；Agent/Evidence 共用所选 session 流；单位策略、Admin 与单 run 删除确认在响应不确定时保留同一请求字节及幂等 key，明确拒绝后重新预览。新增数据库故障、重试、历史视图与运行中 Evidence 回归；专用 PostgreSQL runner **5 passed**、application **1023 passed**、production PG/Chrome **2 passed**，frontend **42 passed**、Chrome fake **7 passed、1 production-only skipped**，lint、typecheck、OpenAPI drift、build 通过。未运行真实外部 provider 或跨平台验收。
- 11 份相关文档链接与代码围栏均 0 问题，contract matrix 30 条、27 implemented / 3 partial；15 个本阶段源码/文档文件凭据特征扫描 0 命中，frontend bundle 边界检查通过。旧目录的部分 pytest 临时文件存在 ACL 限制，未扫描或提交这些目录；仅按列出的路径暂存本阶段文件。
- 真实 OpenAI/Google provider、三个 OS credential backend 实机、容量与 Ubuntu/macOS CI **not run**；Windows 本机 fake/PG/Chrome 不能替代它们。原因是需要明确授权/可用凭据或对应平台；matrix 的 GLOBAL-C11、M9-C01、M9-C03 保持 partial。本地提交见该阶段 HEAD；未 push。

## Contract 承接矩阵（实施时填写）

状态只能使用 `pending / partial / implemented`。只有 production implementation、对应测试、文档和阶段完成证据齐全时才能改为 `implemented`；真实 provider、三平台或容量未运行时，对应外部边界保持 `partial`/`not run`，不能被fake、本机或文档完成替代。

| Contract | 当前状态 | 主要阶段 | 最低证据要求 | 实施位置 / 测试 / 文档 |
|---|---|---:|---|---|
| GLOBAL-C01 | implemented | 0/7/9 | archive isolation architecture/source/build audit | `backend/app/`, `frontend/src/`；`backend/tests/architecture/test_archive_isolation.py`、`frontend/scripts/check-boundaries.mjs`；本文件红线与[实施设计](m9_implementation_design.md)。 |
| GLOBAL-C02 | implemented | 1/9 | current-only V1，无compat/flag/dual path | `backend/app/application/analysis_runs/models.py`、`backend/app/api/http/`；`backend/tests/architecture/test_current_v1_contracts.py`；[API](m9_settings_admin_api.md)。 |
| GLOBAL-C03 | implemented | 3/7 | COMID与真实session identity不泛化 | `backend/app/application/analysis_runs/topology_driven_workflow.py`、`frontend/src/features/analysis/TopologyMap.tsx`；`backend/tests/smoke/test_m9_production_pg.py`、`frontend/e2e/workspace.spec.ts`；[Frontend](m9_frontend_experience_design.md)。 |
| GLOBAL-C04 | implemented | 3/7 | UTC事实、原IANA snapshot/history | `backend/app/application/settings_admin/runtime_settings.py`、`backend/app/application/transcripts/reader.py`；`backend/tests/smoke/test_m9_production_pg.py`；[Policy](m9_settings_admin_policy_catalog.md)。 |
| GLOBAL-C05 | implemented | 3/5 | DST/window与单位snapshot不被current setting改写 | `backend/app/application/settings_admin/unit_policy_service.py`、`backend/app/application/reasoning/`；`backend/tests/api/test_m9_settings_postgres.py`、`backend/tests/application/test_m9_unit_preferences.py`；[Policy](m9_settings_admin_policy_catalog.md)。 |
| GLOBAL-C06 | implemented | 5/7/9 | result/evidence/source refs闭合 | `backend/app/application/evidence_tools/`、`backend/app/application/reasoning/`；`backend/tests/smoke/test_m9_production_pg.py`、M5/M6 smoke；[总设计](m9_settings_admin_demo_design.md)。 |
| GLOBAL-C07 | implemented | 4/8/9 | secret不进公开/日志/持久化/browser | `backend/app/application/settings_admin/credential_service.py`、`frontend/src/features/settings/SettingsDrawer.tsx`；`backend/tests/api/test_m9_keys.py`、`frontend/tests/settings.test.tsx`、bundle audit；[API](m9_settings_admin_api.md)。 |
| GLOBAL-C08 | implemented | 0/7/8 | 独立`frontend/`与最终OpenAPI/SSE | `frontend/src/api/`、`backend/app/main.py`；`frontend/tests/sse.test.ts`、`frontend/e2e/production.spec.ts`、`check:api`；[Frontend](m9_frontend_experience_design.md)。 |
| GLOBAL-C09 | implemented | 3/7/9 | backend facts驱动UI，不另造状态/结论 | `frontend/src/features/analysis/models.ts`、`DetailPanel.tsx`；`frontend/tests/state.test.ts`、production browser smoke；[Frontend](m9_frontend_experience_design.md)。 |
| GLOBAL-C10 | implemented | 2/6/9 | ownership、约束、可观察integration evidence | `backend/app/application/settings_admin/admin_service.py`、`backend/app/infrastructure/persistence/`；`backend/tests/api/test_m9_admin_postgres.py`、`backend/tests/smoke/test_m9_production_pg.py`；[Policy](m9_settings_admin_policy_catalog.md)。 |
| GLOBAL-C11 | partial | 2/4/8/9 | 跨平台依赖/migration/build/OS store实测状态 | `backend/app/infrastructure/credentials/os_key_store.py`、migration `20261008_03`、`frontend/package-lock.json`；Windows PG/browser 通过；Ubuntu/macOS、真实 OS store 与跨系统迁移未运行；[实施设计](m9_implementation_design.md)。 |
| M9-C01 | partial | 1–6 | settings/provider/key/admin production composition | `backend/app/main.py`、`backend/app/infrastructure/runtime/provider_bundle.py`；`backend/tests/smoke/test_m9_production_pg.py`、`backend/tests/infrastructure/test_m9_provider_bundle.py`；[总设计](m9_settings_admin_demo_design.md)；真实 provider 未运行。 |
| M9-C02 | implemented | 3/7 | dynamic timezone只影响future，history原identity | `backend/app/application/settings_admin/runtime_settings.py`、`backend/app/application/transcripts/reader.py`；生产 PG 双 run/restart smoke；[Policy](m9_settings_admin_policy_catalog.md)。 |
| M9-C03 | partial | 4 | secret DTO、OS store、masked status、explicit test | `backend/app/infrastructure/credentials/os_key_store.py`、`backend/app/api/http/keys.py`；`backend/tests/api/test_m9_keys.py`、生产 fake-store smoke；[API](m9_settings_admin_api.md)；真实 OS store 后端未实测。 |
| M9-C04 | implemented | 5 | deterministic unit parse + Agent applicability | `backend/app/domain/settings_admin/`、`backend/app/application/reasoning/`；`backend/tests/application/test_m9_unit_preferences.py`、`backend/tests/api/test_m9_unit_policy.py`；[Policy](m9_settings_admin_policy_catalog.md)。 |
| M9-C05 | implemented | 2/5 | global confirmation + append-only version | `backend/app/application/settings_admin/unit_policy_service.py`、`backend/app/infrastructure/persistence/settings_repository.py`；`backend/tests/api/test_m9_settings_postgres.py`、`backend/tests/persistence/test_m9_persistence.py`；[API](m9_settings_admin_api.md)。 |
| M9-C06 | implemented | 2/6 | preview/operation/cleanup/tombstone/ownership | `backend/app/application/settings_admin/admin_service.py`、`admin_operation_runner.py`；`backend/tests/api/test_m9_admin_postgres.py`、production PG smoke；[Policy](m9_settings_admin_policy_catalog.md)。 |
| M9-C07 | implemented | 7–9 | OpenAPI/SSE/frontend/smoke/startup一致性 | `backend/app/main.py`、`frontend/src/api/generated.ts`；`backend/tests/api/test_m9_target_openapi.py`、`frontend/e2e/production.spec.ts`、`check:api`；[API](m9_settings_admin_api.md)、`backend/README.md`。 |
| M9-C08 | implemented | 7/8 | new frontend、两层fetch SSE、三栏与状态所有权 | `frontend/src/app/App.tsx`、`frontend/src/api/sse.ts`；`frontend/tests/sse.test.ts`、`frontend/e2e/workspace.spec.ts`；[Frontend](m9_frontend_experience_design.md)。 |
| M9-C09 | implemented | 0/6/8/9 | single process/worker、loopback same-origin | `backend/app/__main__.py`、`backend/app/api/http/local_mutation.py`；`backend/tests/smoke/test_m9_production_pg.py`、`backend/tests/api/test_m9_frontend_static.py`；`backend/README.md`。 |
| M9-C10 | implemented | 7/9 | map/result/summary/Agent/Evidence真实facts | `frontend/src/features/analysis/TopologyMap.tsx`、`DetailPanel.tsx`；production browser smoke、`frontend/tests/detail-panel.test.tsx`；[Frontend](m9_frontend_experience_design.md)。 |
| M9-C11 | implemented | 7/9 | persistence retry入口与状态独立 | `backend/app/api/http/analysis_runs.py`、`frontend/src/features/analysis/DetailPanel.tsx`；production PG smoke、`backend/tests/api/test_analysis_history.py`、`frontend/tests/detail-panel.test.tsx`；[Frontend](m9_frontend_experience_design.md)。 |
| M9-A01 | implemented | 3–6/9 | settings/key/admin tests与secret audit | `backend/app/api/http/{settings,keys,admin}.py`；`backend/tests/api/test_m9_settings.py`、`test_m9_keys.py`、`test_m9_admin_postgres.py`；[API](m9_settings_admin_api.md)。 |
| M9-A02 | implemented | 6/9 | cleanup后keys/snapshot不变、tombstone与RAG ownership | `backend/app/application/settings_admin/admin_service.py`；`backend/tests/api/test_m9_admin_postgres.py`、production PG restart/cleanup smoke；[Policy](m9_settings_admin_policy_catalog.md)。 |
| M9-A03 | implemented | 7/9 | create到complete/cancel/history E2E | `backend/app/main.py`、`frontend/src/app/App.tsx`；两项 production PG/Chrome smoke、`frontend/e2e/workspace.spec.ts`；[总设计](m9_settings_admin_demo_design.md)。 |
| M9-A04 | implemented | 7/9 | 三态结果、EvidenceChain、confidence、attribution | `backend/app/application/reasoning/`、`frontend/src/features/analysis/DetailPanel.tsx`；M6 smoke、production PG/Chrome smoke、`frontend/tests/detail-panel.test.tsx`；[Frontend](m9_frontend_experience_design.md)。 |
| M9-A05 | implemented | 7–9 | frontend/OpenAPI/SSE/error/smoke同名同义 | `frontend/src/api/{http,sse,generated}.ts`、`backend/app/api/`；`check:api`、`frontend/tests/{http,sse}.test.ts`、production browser smoke；[API](m9_settings_admin_api.md)。 |
| M9-A06 | implemented | 3/7/9 | history list字段来自persisted facts | `backend/app/application/transcripts/reader.py`、`frontend/src/features/history/HistoryList.tsx`；`backend/tests/api/test_analysis_history.py`、production PG restart smoke；[Frontend](m9_frontend_experience_design.md)。 |
| M9-A07 | implemented | 7/9 | 每参与水体结果/上下文/full content一致 | `backend/app/application/transcripts/reader.py`、`frontend/src/features/analysis/DetailPanel.tsx`；`backend/tests/smoke/test_m9_production_pg.py`、`frontend/tests/detail-panel.test.tsx`；[Frontend](m9_frontend_experience_design.md)。 |
| M9-A08 | implemented | 3/7/9 | retry保存不重跑、预算不重置、warning真实 | `backend/app/application/analysis_runs/writer.py`、`frontend/src/features/analysis/DetailPanel.tsx`；`backend/tests/api/test_analysis_history.py`、production PG retry no-op、frontend unit；[Frontend](m9_frontend_experience_design.md)。 |

## 每个 Milestone 的完成记录模板

```text
Milestone N completed on: YYYY-MM-DD
Branch / base / HEAD / upstream:
Applicable contracts:
Production changes:
Migration revision / database state:
Frontend dependency lock / generated API state:
Tests added or changed:
Verification commands and exact results:
PostgreSQL isolation / restart evidence:
Provider / OS credential backend / platform / capacity evidence (pass, fail, skipped, not run):
Failure found and repair performed:
Docs and OpenAPI synchronized:
Secret / archive / artifact audit:
Commit SHA:
Remaining risks / next-stage handoff:
```

禁止仅填写“tests pass”“done”或复制上一阶段数字。无法提供production implementation、focused test、阶段出口与completion evidence的复选框保持未勾选；文档创建、route存在、页面能打开或命令exit 0都不单独构成完成证据。
