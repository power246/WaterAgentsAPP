# M9 Settings/Keys/Admin + Frontend 实施设计

本文描述 M9 的模块、依赖、持久化、runtime 重组、前端构建和验证边界。第 1 节保留设计冻结时的接入基线；实际完成项和未运行边界见 [Implementation TODO](m9_implementation_todo.md)。总决策见 [M9 总设计](m9_settings_admin_demo_design.md)，policy 与 wire contract 分别见 [Policy Catalog](m9_settings_admin_policy_catalog.md) 和 [API](m9_settings_admin_api.md)。

---

## 1. 当前接入点与缺口

当前 production 已有：

- `create_app`、统一 error envelope/request correlation；
- create/list/detail/history/transcript/content/cancel/retry routes；
- run snapshot SSE 与 session process SSE；
- PostgreSQL/Alembic transcript/history 与 M8 RAG schema；
- `RAGMaintenanceOperations` 内部 typed health/build/retry/retire/delete/history cleanup/profile handoff；
- deployment `AppSettings`、model catalog、parameter/unit registry；
- `KeyStorePort.get/set/delete` 协议接缝。

缺口：

- `frontend/` 当前只有空目录，尚无应用源码、manifest 或 lockfile；
- settings/keys/admin production routes/DTO/repositories 不存在；
- mutable defaults、unit policy version、Admin operation/confirmation、deleted idempotency tombstone 无 schema；
- `KeyStorePort` 无 production OS adapter/masked public model；
- M4/M6/M8 provider adapters在 startup 读取 `AppSettings` secret，不能满足“只影响未来 run/attempt”；
- backend 未托管 production frontend；
- 当前无持久化 evidence cache，不应实现假 cleanup。

## 2. 依赖方向

```text
frontend
  -> HTTP/SSE/OpenAPI only

api/http + api/schemas
  -> application/settings_admin
  -> existing application/transcripts + transcript_memory

application/settings_admin
  -> domain settings/unit/admin models
  -> ports: settings store, key store, admin store, clock/id factory
  -> RAGMaintenanceOperations (existing application service)

infrastructure
  -> PostgreSQL repositories
  -> OS credential adapter
  -> provider/runtime factories
  -> static frontend hosting
```

domain/application 不 import FastAPI、SQLAlchemy、keyring、Vite 文件或 `AppSettings`。API route 不直接执行 SQL/OS keyring/M8 repository。Admin service 必须调用 compose 后的同一 `RAGMaintenanceOperations` 实例，不能创建第二个 reader-lease owner/repository graph。

## 3. 建议模块

```text
backend/app/
  domain/settings_admin/
    settings_models.py
    credential_models.py
    unit_policy_models.py
    admin_operation_models.py
    confirmation_models.py
  application/settings_admin/
    settings_service.py
    credential_service.py
    unit_policy_service.py
    admin_service.py
    operation_runner.py
    runtime_resolver.py
  ports/
    settings_store.py
    admin_store.py
    key_store.py              # 扩充 typed value/availability，保留窄 get/set/delete
  api/schemas/settings_admin.py
  api/http/settings.py
  api/http/keys.py
  api/http/admin.py
  infrastructure/credentials/os_key_store.py
  infrastructure/persistence/settings_admin_repository.py
  infrastructure/runtime/m9.py
  infrastructure/web/frontend.py

frontend/
  src/api/
  src/app/
  src/features/analysis/
  src/features/history/
  src/features/settings/
  src/features/admin/
  src/components/
  src/styles/
```

若实现证明更少文件即可保持边界，可合并单一用途模块；不得为了匹配目录图创建空 wrapper/interface。

## 4. Domain/application types

核心类型至少包括：

```text
MutableDefaults  # 不含 active_unit_policy_version
SettingsSnapshot(schema_version, revision, defaults, updated_at)
SettingsCatalogProjection

CredentialName(openai | google_geocoding)
StoredCredential(value, updated_at)
CredentialSource(user_store | deployment | none)
CredentialStatus
CredentialLease

UnitPolicyVersion
UnitPolicyEntry(parameter_key, target_unit_id)
UnitPolicyPreview
SessionUnitOverride

AdminActionKind
AdminPreview
AdminConfirmation
AdminOperation(state/progress/safe result/error)
IdempotencyTombstone
```

DTO 与内部 secret-bearing type 分离。`StoredCredential`/`CredentialLease` 不可序列化进公开 model；masked projection 由 application service 显式构造，不依赖 `SecretStr.__str__` 偶然行为。

## 5. PostgreSQL schema

新增一个 Alembic head，在现有 M8 head 上前进；不得修改已发布 migration。

### 5.1 `runtime_settings`

singleton row，建议显式 typed columns：

```text
singleton_id = 1 PK/check
schema_version
revision UNIQUE, >= 1
analysis_timezone
parser_provider / parser_model
waterbody_provider / waterbody_model
memory_mode
time_resolution
desired_embedding_profile_id
active_unit_policy_version FK
updated_at
```

read uses deployment defaults when row absent；insert/update 使用 compare-and-swap `WHERE revision = expected`。不得把完整 `AppSettings` dump 存 JSON。

### 5.2 `unit_policy_versions`

```text
version BIGINT PK generated/sequence
registry_digest
created_at
```

### 5.3 `unit_policy_entries`

```text
version FK unit_policy_versions ON DELETE RESTRICT
parameter_code_system
parameter_code
target_unit_id
PRIMARY KEY(version, code_system, code)
```

旧 version 不删除。migration 创建 version 1 时，从当前 capability `unit_policy_id` 生成 entries，并让首次 settings/default projection引用 version 1；registry digest 必须和启动 registry 一致。

### 5.4 `analysis_run_idempotency_tombstones`

当前 key/fingerprint/receipt 与 run row同表。history delete 前将有 key 的 run 原子转写：

```text
idempotency_key UUID PK
request_fingerprint BYTEA(32)
deleted_run_id UUID UNIQUE
deleted_at
```

然后删除 run facts。create lookup 先/同事务检查 live `analysis_runs` 和 tombstone，二者对同 key互斥。tombstone 不保存 receipt/body/query。

### 5.5 `admin_operations`

```text
operation_id UUID PK
kind
state
resource_identity
request_fingerprint BYTEA(32)
idempotency_key UUID UNIQUE
confirmation_token_digest BYTEA(32) UNIQUE
safe_request_summary JSONB
processed/total/succeeded/failed counts
safe_result JSONB nullable
safe_error JSONB nullable
created_at / started_at / finished_at
```

JSONB 只承载有界 discriminated safe summaries/results，并在 application/Pydantic 边界严格校验；不能放任意 provider payload。partial unique index 保证同 `kind + resource_identity` 最多一个 pending/running operation。

preview token 本身只存在于有界进程内 registry；operation 的唯一 token digest 保证同一确认不能创建两个任务。进程重启使尚未执行的 preview 失效，不新增 confirmation table。

### 5.6 不新增 credential table

application-managed credential envelope `{schema_version, value, updated_at}` 整体存进 OS credential manager。这样 secret 与 metadata 在同一安全 backend 原子替换，避免假设 OS keyring 与 PostgreSQL 有跨系统事务。PostgreSQL 不保存 ciphertext、last four 或 user secret value。

## 6. OS credential adapter

production 增加 `keyring` 类跨平台 library并锁定版本：

- Windows 使用 Credential Manager backend；
- macOS 使用 Keychain backend；
- Linux 只在可用的 Secret Service/keyring backend 下启用；headless/no backend 明确 unavailable；
- 禁止 plaintext/fail-safe file backend；adapter startup probe 必须识别 insecure/no backend；
- service name固定且 namespaced，account 只接受 typed allowlist；
- tests 使用 in-memory fake，不读写开发者真实 keychain。

`KeyStorePort` 可以保留 generic get/set/delete 形状，但 production adapter value 应为 typed stored envelope。availability/error 映射成 application-level `credential_store_unavailable`，不能把 library exception/public path 泄漏到响应。

effective resolver先读 user store；没有 override 再读 startup deployment `SecretStr`。读取 secret 只在构造 lease/test request 时展开，并把值加入现有 private-value redaction boundary。

## 7. Dynamic settings 与 run materialization

不能在每次组件 render 或每个 tool call读取 mutable settings。创建链增加 `RuntimeSettingsResolver`：

1. 从 settings repository读取 current/default snapshot；
2. 对 create request 的显式 model/memory/resolution再次用 deployment catalog 校验；
3. 读取 active unit policy version/registry digest；
4. 取得该 run 所需 effective credentials，构造 immutable leases；
5. 生成扩展 `AnalysisConfigurationSnapshot` 与 per-run provider bundle；
6. 在接受 run/persist creation前完成必须的配置可用性检查。

只有 configuration identity进入 transcript；secret lease 不进入 snapshot/persistence。创建失败必须保持现有原子语义：不能留下半个 run/idempotency receipt。

mutable defaults 中的 model/memory/resolution主要用于 frontend预填；create body仍显式提交。timezone/unit policy由 server在创建瞬间固化。

## 8. Provider runtime 重组

当前 M4/M6/M8 production builders 在 startup 用 `AppSettings` secret构造长期 adapter。M9 应拆分：

### 8.1 Application-owned shared resources

继续单例复用：

- HTTP client/pool（不得携带固定 Authorization/default secret header）；
- database engine/repositories；
- parameter/model catalogs、hard limits；
- M5 public data adapters（USGS deployment config）；
- M8 generation repository、reader lease、job worker协调器；
- run coordinator/live stores/event writer。

### 8.2 Per-run provider bundle

`RunProviderBundleFactory` 接收 run configuration + credential leases，构造：

- query parser model adapter；
- Google geocoding adapter；
- waterbody/run-summary model adapter/factory；
- M8 session query-embedding adapter，使用同 run OpenAI lease。

bundle 与 run lifecycle一起释放；不新建 database engine/全局 worker。不同 run 可使用不同 key/settings snapshot，existing run不受 replace/delete影响。

workflow/coordinator 接缝应接收 `AnalysisWorkflowFactory`/per-run execution context，而不是用 ContextVar 或修改全局 adapter field。test composition也走同一 factory，只注入 fake leases/adapters。

### 8.3 Background attempt lease

M8 index worker不是某个 run的执行部分。每个 claimed embedding batch attempt开始时从 effective resolver取得一次 lease，构造/借用无固定 auth header的 adapter；attempt 中不切换。下一 attempt才读新 key。missing key让 job进入既有 safe retry/failure路径，不记录 key。

## 9. Unit intent 与 reasoning 接入

M9 扩展 query-understanding typed output，保留 bounded user unit intent（raw alias +可选 parameter reference/rationale），但不让 parser定义 formula或直接扩大 parameters。

`UnitPreferenceResolver`：

1. 用 registry normalization解析 alias；
2.按 dimension和 sealed `AnalysisContext.params_used/supporting_params` 生成候选；
3. unknown/ambiguous/incompatible返回 stable typed issue；
4.把允许的 target unit ids加入 waterbody reasoning task/skeleton；
5. Agent只在候选中判断 parameter applicability；
6. conversion仍由现有 `convert_evidence_unit`/decimal policy执行；
7. validator确保 result `target_unit_id` 与允许候选及 evidence closure一致；
8. typed decision/session result记录 override source/registry digest。

global policy entries形成没有 query override时的 server target preference；明确 session override优先于 global entry。两者都不能修改 source records或 M5 scientific gates。

## 10. Settings/key services

`SettingsService` 负责：

- compose deployment initial defaults；
- role-filtered safe catalog projection；
- ordinary defaults full replacement + revision CAS；active unit policy只由专用 confirmation transaction更新；
- timezone/model/profile/unit policy validation；
- unit policy preview/confirm/version transaction。

`CredentialService` 负责：

- typed allowlist和 effective source；
- save/delete/masked projection；
- deployment fallback；
- explicit fixed test connection；
- lease issuance。

route 只处理 HTTP/header/status；不在 route中 mask value、访问 env或操作 keyring。

## 11. Admin service 与 operation runner

`AdminService` 负责有界进程内 preview/confirmation registry 与持久化 operation idempotency；`AdminOperationRunner` 只执行已提交 operation。

提交顺序：

1. 在 registry lock 下 validate token/action/idempotency；
2. 重新读取事实并比较 fingerprint；
3. 在 DB transaction中以 unique token digest + idempotency key insert/return operation；
4. commit后才 schedule task；
5. task CAS `pending -> running`；
6. 调用 action handler；
7. 写 terminal safe result/error。

相同 idempotency key +相同 bytes返回原 operation；不同 bytes conflict。若 process在 commit后、schedule前崩溃，startup reconciliation看到 pending并重新 schedule；启动时遇到遗留 running operation，则按 action durability收敛：M8 durable jobs可继续观察，非 durable history transaction若未提交则标记 interrupted，并要求重新 preview。

history delete handler直接复用 `RAGMaintenanceOperations.inspect_cleanup_dependencies/delete_history_runs` 的同一 store/transaction边界，并扩展 idempotency tombstone transaction。若现有 `delete_history_runs` transaction不能同时写 tombstone，扩充 store method/transaction，而不是先删后补。

profile handoff/retry/delete handler复用 existing typed methods，不复制 generation state machine。

## 12. Static frontend hosting

production build output 为 `frontend/dist/`。backend frontend responder：

- API routers和 SSE先注册；
- 只对非 `/api` 的 GET/HEAD及 HTML navigation做 index fallback；
- 真实 asset存在时按 MIME返回；hashed assets可 immutable cache，`index.html` no-cache；
- path traversal/目录读取拒绝；
- dist missing时 API可构造用于 tests/CLI，但访问 UI返回明确 503/404并由 health/启动日志安全报告；
- 不得读取 `frontend_bk/` fallback。

Vite dev proxy保持 SSE streaming，不buffer、不修改 event bytes；业务 client只用相对 URL。

## 13. Frontend implementation choices

首版使用：

- React + TypeScript + Vite；
- React Router用于可恢复 URL；
- Leaflet/react-leaflet；
- Recharts 或等价受控 chart library，只渲染 backend series；
- 一个成熟的 SSE frame parser或等价经过 parser fixture验证的单一实现；
- CSS variables/grid + feature styles，不引入第二套 design system强制重排。

API types 从 backend OpenAPI生成并提交/在 CI检查 drift；handwritten client只承载 HTTP/SSE policy，不复制 response DTO。若 session SSE union未完整进入 OpenAPI，先让 backend export schema，再生成前端类型；不以 `any`/unchecked cast绕过。

server state按 feature reducer隔离：run snapshot replacement、session transcript assembly、settings draft、admin operation分别维护。第一版不引入全局 event bus/Redux/plugin system；若实现中确有跨 route server-cache需求，必须先证明 React context/reducer无法保持 identity。

## 14. API/error 集成

新增 exception 进入现有 error handler/`AnalysisRunErrorResponse`：

- safe field violation沿用 `validation[]`；
- settings/key/admin identity加入 `_SAFE_FIELDS`；
- no-store/X-Request-ID/structured logs保持；
- secret-bearing exception在进入 logger前通过现有 redaction/private values处理；
- frontend按 `error.code`/HTTP status处理，不解析 message。

create handler新增 tombstone lookup：same bytes抛 typed deleted error映射 410；different bytes继续 409。OpenAPI同步新增 response。

## 15. Migration/compatibility

M9 是 current backend schema向前演进，不兼容 `backend_bk/frontend_bk`。必须保持当前 M1–M8 persisted history可读：

- migration不重写 immutable transcript body；
- 旧 run configuration无 unit policy fields时reader投影 explicit legacy/unknown；
- 第一次 M9 run写完整新 snapshot/schema；
- 现有 analysis_runs/idempotency rows继续正常；只有用户实际 cleanup时生成 tombstone；
- M8 active/building/retired generations不重建即可被 Admin health读取；
- 没有 settings row时使用 validated initial defaults。

不增加 dual-write到旧格式或旧 route compatibility layer。

## 16. Testing strategy

### 16.1 Domain/application unit

- settings revision/default composition/catalog validation；
- unit alias/dimension/applicability/global version/session override；
- credential allowlist/source precedence/masking/lease isolation；
- confirmation token digest/expiry/stale/single-purpose/consumption；
- operation state/idempotency/conflict/restart reconciliation；
- deleted idempotency same/different bytes；
- profile handoff action mapping复用 M8 state。

### 16.2 Infrastructure

- PostgreSQL migration head/up/down boundary按项目既有方式；
- singleton CAS并发、unit version append-only、operation token digest/idempotency unique constraints；
- history cleanup + consumption graph + tombstone同事务rollback；
- OS credential adapter用 fake backend验证 serialization/error/mask，绝不碰真实开发者 credential；
- static responder path/MIME/cache/fallback/API isolation。

### 16.3 API contract

- 每条新增 route success/status/header/OpenAPI；
- unknown fields/query/content-type/origin/cross-site；
- secret不出 response/log/transcript/event；
- settings revision、key deployment fallback/test connection、Admin preview/token/202/poll；
- evidence-cache route不存在；
- create tombstone 410/409；
- 现有 analysis/history/two SSE regression全过。

### 16.4 Frontend

建议 Vitest + Testing Library + Mock Service Worker做 unit/integration，Playwright做浏览器 E2E；具体 lock版本由 `frontend/package-lock.json` 固定。

- OpenAPI type generation无 drift；
- fetch-SSE跨 chunk parser/stream_end/400/409/410；
- GET/SSE交叠、message原位增量、切水体释放旧 session stream；
- 地图状态、三 tab、history cursor/timezone、settings conflict/key mask；
- Admin preview/confirmation/poll/reopen；
- production dist由 backend同源提供。

### 16.5 E2E 与 evidence lanes

1. offline unit/contract；
2. PostgreSQL integration/restart；
3. deterministic fake browser E2E完整流程与 cancel分支；
4. real OpenAI/Google opt-in minimum smoke；
5. Windows/Ubuntu/macOS可用门禁；
6. capacity/long-run另行证据。

各 lane单独记录 command、environment、result和限制。fake不能替代 real provider；单一真实请求不能替代容量；本机通过不能替代三平台。

## 17. 发布门禁

本地实现完成至少需要：

- migration/repositories/settings/key/admin/API/frontend/static hosting落入唯一 production composition；
- 完整 backend regression、frontend unit/build、fake browser E2E通过；
- OpenAPI生成与前端消费无 drift；
- secret scan无泄漏；
- history/index/key cleanup隔离与 tombstone验证；
- M8 active generation/search、M7 workflow、M6 result、M3 history/SSE回归未倒退；
- 启动说明明确 PostgreSQL、build、单 process/worker、loopback URL和真实调用开关。

真实 provider、三平台或容量未运行时，交付记录必须保留 `not run/partial`，不得把“代码存在”写成“外部验证完成”。

## 18. 建议施工顺序与提交边界

这不是 TODO；实现者可在不破坏依赖方向的前提下调整，但每个边界应独立可验证：

1. **Contract/domain**：M9 types、OpenAPI目标 schema、architecture tests；
2. **Persistence**：migration、settings/unit/admin/tombstone repositories与 PostgreSQL tests；
3. **Settings/units**：services、snapshot扩展、reasoning unit接入与 focused regression；
4. **Credentials/runtime**：OS adapter、resolver、per-run/attempt provider factory与 secret regression；
5. **Admin**：preview/confirmation/operation + M8 maintenance exposure + cleanup transaction；
6. **Frontend shell/client**：routes、typed HTTP/SSE、layout/state；
7. **Frontend features**：analysis/map/detail/history/settings/admin；
8. **Production closure**：static hosting、browser E2E、docs/OpenAPI sweep、evidence matrix。

不得先做漂亮 UI再补 contract，也不得一次提交混入旧 frontend/backend、无关重构或真实 provider artifact。
