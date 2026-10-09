# M9 Settings / Keys / Admin Policy Catalog

本文是 M9 application policy 的设计输入。runtime 不解析 Markdown；规则由 typed models、validators、services、repositories、HTTP schemas 和 tests 承接，实际实施证据见 [M9 Implementation TODO](m9_implementation_todo.md)。总体边界见 [M9 总设计](m9_settings_admin_demo_design.md)，可序列化接口见 [M9 API](m9_settings_admin_api.md)。

---

## 1. 权威来源与优先级

同一配置不得由多个可写来源竞争。优先级按用途定义：

1. deployment settings：进程/基础设施、安全硬边界和 allowlist；
2. PostgreSQL mutable settings：本地用户可修改的默认值；
3. create request：本次 run 明确选择的模型、memory mode、resolution 和可选 target/date；
4. run configuration snapshot：run 创建后唯一权威，不再回读当前 defaults；
5. session unit override：只覆盖对应 session/参数的展示目标单位，不改全局 policy 或原 evidence；
6. credential lease：一次 run/外部 attempt 开始时取得的不可变进程内 secret 值。

前端显示默认值不是权威。backend 必须在 create/save/action 边界重新验证 catalog、revision、profile、unit dimension、dependency 和确认 token。

## 2. Settings 分类

### 2.1 第一版可写 mutable defaults

| 字段 | 来源/选择约束 | 生效点 |
|---|---|---|
| `analysis_timezone` | 有效 IANA identity | 新 run 创建时固化 |
| `parser_model` | deployment model catalog 的 parser role | 前端预填；create body 必须显式提交 |
| `waterbody_model` | deployment model catalog 的 waterbody role | 前端预填；create body必须显式提交 |
| `memory_mode` | backend enum | 前端预填；create body必须显式提交 |
| `time_resolution` | backend enum | 前端预填；create body必须显式提交 |
| `desired_embedding_profile_id` | deployment embedding catalog | 只表达期望；不自动激活 generation |
| `active_unit_policy_version` | versioned unit policy；只由专用确认流程更新，不属于普通 Settings PUT | 新 run 创建时固化 identity/digest |

parser/waterbody model、memory mode、resolution 虽然来自 settings 预填，仍必须由 create request 显式传回并再次校验。这样 request bytes/idempotency 和 run snapshot 保持完整，不依赖提交后可能变化的 UI 状态。

### 2.2 deployment-only

以下字段不可出现在可写 Settings DTO：

- environment、log level、host、port；
- PostgreSQL host/port/name/user/password/SSL/pool；
- provider base URL、model capability、context/output limit、embedding dimensions/distance/template identity；
- topology/evidence/reasoning/RAG worker timeout、concurrency、attempt、batch、page、record、byte 和 retention hard limits；
- parameter capability manifest 与 unit conversion formula；
- CORS/origin/Host、安全日志与 secret backend 选择；
- USGS optional deployment credential。

GET settings 可以返回 UI 需要的只读 catalog/limits，但不得把它们伪装为 mutable fields。deployment catalog 变更经启动校验和重启生效。

### 2.3 current-settings record

PostgreSQL 只保留一条 current-settings record：

```text
schema_version
revision
analysis_timezone
parser_model
waterbody_model
memory_mode
time_resolution
desired_embedding_profile_id
active_unit_policy_version
updated_at
```

- database 无记录时，从 validated deployment defaults 组成 revision `0` 的 effective response；
- 第一次成功保存插入 revision `1`；后续完整替换要求 expected revision 匹配并递增；
- `schema_version` 由 server 管理，client 不选择；
- 不建设通用 settings history、rollback、scheduled activation 或 per-user row；
- DB unavailable 时读取/写入 mutable settings 返回明确 unavailable；application construction 本身仍可使用 deployment settings。

## 3. Settings 保存 policy

PUT 是普通可写 defaults（timezone、models、memory mode、resolution、desired embedding profile）的完整 replacement，不是 JSON merge patch。`active_unit_policy_version` 只读展示并由专用 unit-policy confirmation 更新，不能出现在普通 PUT body。server 按固定顺序：

1. strict JSON/unknown-field validation；
2. model/memory/resolution/timezone/profile/unit policy validation；
3. expected revision comparison；
4. 单事务写入 current record；
5. 返回 server-normalized new record/catalog projection。

revision 不匹配返回 `409 settings_revision_conflict` 和当前安全 settings snapshot。server 不做 last-write-wins，frontend 不做字段级自动 merge。

保存 settings 不调用 provider、不构建 generation、不更改 active generation、不重写 run/session/transcript。desired embedding profile 改变只让 Admin 显示 handoff required。

## 4. Run snapshot 隔离

run 创建时固化：

- analysis timezone；
- request 中明确的 resolution/model/memory mode；
- active unit policy version + registry digest；
- 实际使用的 model identity/capability snapshot；
- 适用的服务端 policy/version identities。

公开 snapshot 不含 secret、masked hint、credential source/version、lease identity 或 OS-store metadata。已有 run 不因 settings save、key replace/delete、model catalog restart 或 unit policy update改变。

历史 UI 必须显示原 `analysis_timezone`。时间点使用该 IANA zone 在对应 instant 的 offset；不得用当前浏览器 timezone/current settings 覆盖。

## 5. Unit policy

### 5.1 Registry authority

`ParameterCapabilityRegistry`/manifest 仍是 unit identity、alias、dimension、scale、offset、kind、precision 和参数适用性的唯一权威。M9 不允许用户新增 formula、unit id、alias 或 dimension。

每个 target unit 必须：

- 存在于当前 registry；
- 与 source/parameter unit 的 `dimension_id` 相同；
- 对 identity-only unit 只允许相同 identity；
- 使用既有 decimal + half-even conversion，不用 JavaScript float 重新实现；
- 保存 registry digest/formula id 以便审计。

### 5.2 单次 session override

用户在 query 中表达单位偏好时，query-understanding 只产出有界 typed unit intent；deterministic resolver 解析 unit alias、列出 dimension-compatible parameter candidates。waterbody Agent 只能在 sealed parameter scope 内判断哪些候选参数适用，并通过现有 allowlisted conversion tool 请求具体 `target_unit_id`。

最终适用选择以 typed decision/transcript 记录：parameter key、target unit id、registry digest、来源 `session_override`。unknown unit、ambiguous dimension、无适用参数或不兼容量纲必须返回稳定限制，不猜 formula、不扩大 parameter scope。

session override：

- 只影响本 session 的转换 evidence 与 result unit；
- 不修改 raw source record；
- 不修改同 run 的其他 session；
- 不自动升级为 global default；
- 不允许 frontend 对最终值二次换算。

### 5.3 全局 target unit policy

全局 policy 是 append-only version record，不属于通用 settings history：

```text
unit_policy_version
registry_digest
parameter_key -> target_unit_id entries
created_at
```

更新前 backend 返回 normalized preview：changed entries、affected parameter/dimension、old/new symbol。用户显式确认后创建新 version 并将 current settings 指向它；旧 version 不删除，因为历史 run snapshot 仍可能引用。

确认 token 必须绑定 base version、new normalized entries 和 registry digest。registry/settings 变化后 token 失效。历史 run 使用原 policy version，不按当前 policy 重标或重算。

## 6. Credential allowlist 与来源

第一版固定两项 application credential：

| key identity | 用途 | UI 可保存 |
|---|---|---|
| `openai` | parser、waterbody reasoning、run summary、embedding | 是 |
| `google_geocoding` | location resolution | 是 |

USGS 保持 deployment-only optional credential；Google Maps、Grok、任意 provider name/base URL 不进入 API。

effective credential resolution：

1. OS user credential store 中的 application override；
2. 对应 deployment environment secret；
3. absent。

UI 保存创建/替换 user-store override；删除只删除 override。如果 deployment secret 仍存在，删除后 status 变为 `configured/source=deployment`。UI 不能读取、替换或删除 deployment secret 本身。

## 7. Secret validation、mask 与存储

写入只接受 strict JSON `{ "value": string }`：trim policy 必须固定；空白/NUL/超过上限拒绝。secret 仅在 request parsing → KeyStorePort → provider lease 的受控路径存在。

禁止进入：

- ordinary settings row/DTO；
- PostgreSQL key metadata；
- run configuration/transcript/SSE/history/admin operation；
- URL/query、browser storage、console、structured log、traceback/public error；
- fixture/snapshot/build artifact。

GET 只返回 configured、source、masked hint、updated_at、store availability 和 allowed actions。masked hint 只能是固定 mask + 最多末四字符；短值仍不得扩大泄漏。DELETE/PUT response 不回显 value。

第一版 KeyStorePort production adapter 使用 OS user credential backend；禁止 plaintext JSON fallback，禁止将自建 encryption key 与 ciphertext 同文件保存。backend unavailable 时：

- deployment secret 仍可读取并报告 configured；
- user-store save/delete 返回 `503 credential_store_unavailable`；
- application 不因没有 key 而无法启动；需外部调用时安全失败。

## 8. Credential lease 与替换/删除

每个 run 在创建/进入 provider runtime 组合时取得所需 credential lease；每个非 run background attempt 在 attempt 开始时取得一次。lease 是进程内不可变 secret，不持久化。

- replace 成功后，新 run/attempt 使用新值；已取得 lease 的执行继续用旧值；
- delete 后，新 run/attempt 若无 deployment fallback 则在外部调用前 provider unavailable；
- 已发出的请求不因 key delete 被标成 cancelled；
- restart 不恢复在途 run，按现有 `service_interrupted` 处理，因此不持久化 lease；
- store 不保留通用 secret version history。

embedding profile handoff 若未来涉及不同 credential identity，必须等 `old_credential_cleanup_ready=true` 后才能清旧 identity。第一版 OpenAI profiles 共用 `openai` identity；profile/model change不能制造第二个任意 credential slot。

## 9. Test connection

保存 key 不自动联网。显式 `Test connection`：

- 使用 effective credential 的一次最小固定 request；
- 固定 allowlisted endpoint/capability/timeout/response byte limit；
- 不接受 user URL、model body 或 arbitrary headers；
- 不写 transcript/history，不保存 raw response；
- 只返回 `succeeded` 或 safe failure code、checked_at、request_id；
- 不证明完整 analysis/RAG/capacity 已验证。

没有 effective credential 返回 stable unavailable，不把 missing key 当成 provider authentication failure。

## 10. Admin authorization 与浏览器写保护

第一版没有登录系统，但 mutation 仍只面向同源本地 UI/明确 CLI：

- 默认 Host 必须是 loopback binding；
- 不开启宽松 CORS；
- 有 body 的 mutation 只接受 `application/json` 和 strict DTO；
- 若存在 `Origin`，必须与请求 origin 完全匹配；
- `Sec-Fetch-Site: cross-site` 的 mutation 拒绝；
- 不得使用 GET mutation、query secret 或 form-urlencoded fallback；
- destructive action 还需要 server preview/confirmation token。

这些保护不是远程多用户 authorization。若未来允许非 loopback 或多用户，必须先增加身份、授权、CSRF 和 per-principal ownership 设计。

## 11. Preview 与 confirmation

destructive action 固定为 preview → confirm execution：

1. preview 在 server 读取当前事实并规范化 selection；
2. 返回影响集合、dependency、counts、warnings、required confirmation 和 opaque token；
3. server 将随机 token 的 digest、action digest、fact fingerprint、required phrase 与 expires_at 放入有界进程内 registry；
4. execution 携带 token 和 UUID v4 `Idempotency-Key`；
5. server 重新读取事实，fingerprint 不同则 `409 admin_preview_stale`；
6. operation row 保存唯一 token digest；同一 token 只能创建一个 operation，同 key 重试返回同 operation；
7. token 过期/用于其他 action/已由不同 key 消费均拒绝。

token 不携带 secret/完整 selection 明文，不替代 transaction 内最终 ownership check。第一版 token TTL 为 server-owned bounded policy，不进入 Settings UI；进程重启清空 registry，未执行 preview 失效并要求用户重新 preview。这符合单 process/worker 边界，不为短期 token 建持久化子系统。

## 12. Admin operation

需要后台执行的动作持久化：

```text
operation_id
kind
state: pending | running | succeeded | failed | interrupted
resource_identity
safe_request_summary
progress counts
safe_error
created_at / started_at / finished_at
idempotency_key + request fingerprint
```

规则：

- operation row 必须先提交，再启动进程内 task；
- 相同 resource/kind 同时最多一个 active operation；
- 冲突 action 在 backend 返回现有 operation/稳定 conflict；
- HTTP disconnect/UI 离开不取消；
- GET status 不产生 side effect；terminal 后不再变化；
- server restart 后不能让遗留记录永久 running；底层状态可 reconcile 才继续，否则收敛 interrupted/failed；
- 不保存 secret、canonical embedding text、vector、raw provider body 或完整 cleanup preview。

前端只在 Admin 可见且 operation active 时有界查询；M9 不新增 Admin SSE。

## 13. History cleanup

selection 为明确 run ids 或 `all_history` snapshot。preview 调用 M8 `inspect_cleanup_dependencies` 并扩展 transcript/idempotency counts：

- 无 retained consumer 引用：按 ownership delete order 删除 requested runs；
- 有 retained consumer 引用且未授权 cascade：preview 标记 cascade required，execution 拒绝；
- 用户明确确认 whole dependent-run cascade：consumer-first 删除整个 dependent runs，再删 sources；
- 不得改写 retained transcript、留下 dangling historical refs 或静默 tombstone source content；
- API keys、current settings、unit policy versions、deployment config 不在删除图。

全量 cleanup 仍使用真实 dependency plan，不用 TRUNCATE 绕过 ownership。operation result 报告 requested/deleted run ids/count、delete order、retained idempotency tombstones 和 safe failures。

## 14. 删除后的 idempotency tombstone

删除 run 与转换 create idempotency row 必须同事务。最小 tombstone 保留：key identity、原 request bytes fingerprint、deleted run identity、deleted state、deleted_at。

- 同 key + 同 bytes：`410 idempotency_resource_deleted`；
- 同 key + 不同 bytes：`409 idempotency_key_conflict`；
- 新 key：正常创建新 run。

tombstone 不含原 body/query/receipt/transcript/result，不出现在 History，不由普通 cleanup 自动删除。live-state cleanup 与 history cleanup 语义必须分开。

## 15. RAG profile handoff 与 generation

desired profile save 不等于 active。公开 Admin workflow复用 M8 `RAGMaintenanceOperations`：

1. inspect handoff；
2. explicit apply 创建 operation；
3. ensure/build desired generation；
4. worker 完成 jobs，完整性验证；
5. 原子 activate desired + retire old active；
6. 等 reader drain 和 retention；
7. 才允许删除 retired generation/旧 credential identity。

building 失败保留旧 active。active generation 不得直接 delete；retired/failed delete 仍受 retention、active replacement 和 reader lease 检查。retry failed jobs 只接受 server safe error-code allowlist 与 bounded limit。

index-only cleanup/rebuild：

- 只处理派生 generation/job/projection rows；
- 不删除 source transcript、history、consumption ownership 或 API keys；
- rebuild 只从原 transcript allowlist 重投影，不从旧 vector/prompt/raw result 恢复；
- 当前没有持久化 evidence cache，不暴露其 cleanup action。

## 16. Action matrix

| Action | Preview | Confirmation | Async operation | 不得影响 |
|---|---:|---:|---:|---|
| Save settings | 否 | revision conflict only | 否 | existing runs |
| Save/replace key | 否 | explicit button | 否 | active leases |
| Delete user key override | 否 | inline confirm | 否 | deployment secret/existing leases |
| Test connection | 否 | explicit cost label | 否 | transcript/history |
| Single/batch/all history cleanup | 是 | 是；cascade 二次确认 | 是 | keys/settings/unit versions |
| Apply RAG profile handoff | 是 | 是；可能产生用量 | 是 | old active before activation |
| Retry failed RAG jobs | impact summary | explicit | 是 | successful/active unrelated jobs |
| Delete retired/failed generation | 是 | 是 | 是 | active generation/source history |
| Clear evidence cache | 不存在 | 不存在 | 不存在 | — |

## 17. Stable failure classes

至少冻结：

```text
settings_revision_conflict
settings_unavailable
credential_not_allowed
credential_store_unavailable
credential_missing
credential_test_failed
unit_unknown
unit_dimension_mismatch
unit_not_applicable
unit_policy_revision_conflict
admin_preview_expired
admin_preview_stale
admin_confirmation_invalid
admin_operation_conflict
admin_operation_not_found
history_cleanup_dependency_conflict
idempotency_resource_deleted
rag_generation_conflict
rag_reader_drain_required
rag_retention_not_elapsed
```

public error 使用现有 `AnalysisRunErrorResponse` shape 或同一共享 error envelope；message 安全稳定，内部 traceback/provider raw body 只进入脱敏后的内部诊断。

## 18. Future gate

后续 milestone 若增加任一能力，必须先扩展本文和根级契约：

- 远程 bind、认证、多用户、角色授权；
- 任意 provider/base URL/model catalog UI；
- 持久化跨 run evidence cache；
- 桌面壳/installer/auto-update；
- 多 worker/shared live state；
- secret sync/rotation/audit history；
- client-defined unit formula。

不得从本机单用户设计直接推断这些能力已安全支持。
