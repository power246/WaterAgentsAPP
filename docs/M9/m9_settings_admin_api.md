# M9 Settings / Keys / Admin API

本文冻结 M9 新增 HTTP DTO、status、错误、幂等与前端消费契约。Settings、Keys 与 Admin backend routes 已在 M1–M6 注册；实施证据见 [M9 Implementation TODO](m9_implementation_todo.md)。已有 analysis/history/SSE contract 保持 [frontend integration contracts](../frontend_integration_contracts.md) 的当前语义。

---

## 1. 通用规则

- prefix 仍为 `/api`；JSON 字段使用 snake_case；时间是 UTC ISO 8601；identity 使用 UUID/string 原义。
- request/response 均由 strict Pydantic models 定义，unknown fields/query parameters 拒绝。
- 有 JSON body 的请求只接受 `application/json`。
- 每个响应保留 `X-Request-ID`；失败复用当前 `AnalysisRunErrorResponse`/`HttpPublicError` envelope。
- mutation 执行同源/Fetch Metadata 检查；不启用宽松 CORS。
- settings、keys、Admin 响应使用 `Cache-Control: no-store`。
- API 不返回 secret、provider raw response、traceback、SQL、vector 或 canonical embedding text。
- 本文样例省略 `X-Request-ID`，实现/OpenAPI 不能省略。

## 2. Route 总表

### 2.1 现有 analysis surface 不改名

```text
POST /api/analysis/runs
GET  /api/analysis/runs
GET  /api/analysis/runs/{run_id}
GET  /api/analysis/runs/{run_id}/sessions/{session_id}
GET  /api/analysis/runs/{run_id}/sessions/{session_id}/transcript
GET  /api/analysis/runs/{run_id}/transcript/{item_id}/content
GET  /api/analysis/runs/{run_id}/events
GET  /api/analysis/runs/{run_id}/sessions/{session_id}/events
POST /api/analysis/runs/{run_id}/cancel
POST /api/analysis/runs/{run_id}/persistence/retry
```

M9 只扩展 create idempotency tombstone 的 `410`，以及 configuration snapshot 的 unit policy identity；不得增加兼容旧 WebSocket 或第三条 SSE。

### 2.2 新增 surface

```text
GET    /api/settings
PUT    /api/settings
POST   /api/settings/unit-policies/preview
POST   /api/settings/unit-policies

GET    /api/keys
PUT    /api/keys/{key_name}
DELETE /api/keys/{key_name}
POST   /api/keys/{key_name}/test

GET    /api/admin/storage
GET    /api/admin/rag
GET    /api/admin/operations
GET    /api/admin/operations/{operation_id}

POST   /api/admin/history-cleanups/preview
POST   /api/admin/history-cleanups
POST   /api/admin/rag/profile-handoffs/preview
POST   /api/admin/rag/profile-handoffs
POST   /api/admin/rag/generations/{generation_id}/failed-jobs/retry-preview
POST   /api/admin/rag/generations/{generation_id}/failed-jobs/retries
POST   /api/admin/rag/generations/{generation_id}/deletion-preview
POST   /api/admin/rag/generations/{generation_id}/deletions
```

不提供 evidence-cache route、arbitrary provider route、direct tool route 或 raw database route。

## 3. Shared models

### 3.1 `ModelOption`

```json
{
  "selection": {"provider": "openai", "model": "registered-model-id"},
  "label": "Registered model",
  "roles": ["query_parser", "waterbody"],
  "available": true,
  "unavailable_reason": null
}
```

只投影 UI 选择所需的安全 catalog metadata；不返回 base URL、secret、内部 deployment field 或未允许 capability。

### 3.2 `EmbeddingProfileOption`

```json
{
  "profile_id": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "label": "Registered embedding profile",
  "available": true,
  "unavailable_reason": null
}
```

profile identity 由 deployment catalog 注册。客户端不能提交 provider/model/dimension/distance/template 的自由组合。

### 3.3 `AdminOperationSnapshot`

```json
{
  "operation_id": "90000000-0000-4000-8000-000000000001",
  "kind": "history_cleanup",
  "state": "running",
  "resource_identity": "history:selection-digest",
  "progress": {
    "processed_items": 2,
    "total_items": 5,
    "succeeded_items": 2,
    "failed_items": 0
  },
  "safe_error": null,
  "created_at": "2026-10-08T01:00:00Z",
  "started_at": "2026-10-08T01:00:01Z",
  "finished_at": null
}
```

`state` 只允许 `pending | running | succeeded | failed | interrupted`。`total_items` 可为 null；counts 必须非负且相互一致。terminal snapshot immutable。kind-specific result 只在 succeeded 时出现，并使用 discriminated union；不能返回 preview token 或 secret。

### 3.4 Confirmation metadata

preview response 共同包含：

```json
{
  "confirmation": {
    "token": "opaque-single-purpose-token",
    "expires_at": "2026-10-08T01:10:00Z",
    "required_phrase": "DELETE 5 RUNS"
  }
}
```

token/phrase 由 server 生成并绑定 normalized action/fact fingerprint。frontend 原样回传，不能构造或解析。

## 4. Settings API

### 4.1 `GET /api/settings`

成功 `200 SettingsResponse`：

```json
{
  "schema_version": 1,
  "revision": 3,
  "defaults": {
    "analysis_timezone": "America/Chicago",
    "parser_model": {"provider": "openai", "model": "parser-model"},
    "waterbody_model": {"provider": "openai", "model": "waterbody-model"},
    "memory_mode": "memory_augmented",
    "time_resolution": "12h",
    "desired_embedding_profile_id": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "catalog": {
    "models": [],
    "embedding_profiles": [],
    "memory_modes": ["stateless", "memory_augmented"],
    "time_resolutions": ["auto", "6h", "12h", "24h"],
    "iana_timezones": ["America/Chicago", "UTC"]
  },
  "unit_policy": {
    "version": 2,
    "registry_digest": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "entries": []
  },
  "updated_at": "2026-10-08T00:00:00Z"
}
```

enum values必须取当前 domain/OpenAPI 实际值；样例不是新增 enum authority。`revision=0` 可表示尚未持久化、由 deployment defaults 组成的有效初值。

### 4.2 `PUT /api/settings`

request 是完整 replacement：

```json
{
  "expected_revision": 3,
  "defaults": {
    "analysis_timezone": "America/Denver",
    "parser_model": {"provider": "openai", "model": "parser-model"},
    "waterbody_model": {"provider": "openai", "model": "waterbody-model"},
    "memory_mode": "stateless",
    "time_resolution": "12h",
    "desired_embedding_profile_id": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
}
```

成功 `200 SettingsResponse`，revision 递增。profile change 只保存 desired identity，不在此 request 内启动外部调用/generation build。`active_unit_policy_version` 不属于本 request；它只出现在 response 的 `unit_policy.version`，并由 §5 的确认 transaction 更新。

错误：

| status/code | 条件 |
|---|---|
| `409 settings_revision_conflict` | expected revision 已过期；error response 附安全 `current_settings` extension |
| `422 request_validation_failed` | timezone/enum/model/profile/unit policy/unknown field 非法 |
| `503 settings_unavailable` | PostgreSQL 不可用 |

`current_settings` 必须使用和 GET 相同的安全 DTO，不能混入 deployment secret。

## 5. Global unit policy API

### 5.1 `POST /api/settings/unit-policies/preview`

```json
{
  "base_version": 2,
  "entries": [
    {
      "parameter": {"code_system": "usgs", "code": "00060"},
      "target_unit_id": "ft3_s"
    }
  ]
}
```

成功 `200 UnitPolicyPreviewResponse`：normalized full entries、changes、affected dimensions、registry digest、confirmation。未知 parameter/unit、dimension mismatch、identity-only conversion 返回 `422`；base version 变化返回 `409 unit_policy_revision_conflict`。

### 5.2 `POST /api/settings/unit-policies`

required header：`Idempotency-Key: UUIDv4`。

```json
{
  "confirmation_token": "opaque-single-purpose-token",
  "confirmation_phrase": "APPLY UNIT POLICY 3"
}
```

成功 `201 UnitPolicyResponse`，包含新 version/registry digest/entries/created_at，并原子更新 current settings 的 active version/revision。相同 key + 相同 bytes 返回原 response；不同 bytes 返回 `409 idempotency_key_conflict`。preview 过期/事实改变分别返回 `409 admin_preview_expired | admin_preview_stale`。

## 6. Key API

`key_name` enum 只允许 `openai | google_geocoding`；未知值返回 `404 credential_not_allowed`，不得暴露内部任意 store lookup。

### 6.1 `GET /api/keys`

`200 KeyStatusResponse`：

```json
{
  "keys": [
    {
      "key_name": "openai",
      "label": "OpenAI",
      "purpose": ["query_parser", "waterbody", "run_summary", "embedding"],
      "configured": true,
      "source": "user_store",
      "masked": "••••abcd",
      "updated_at": "2026-10-08T00:00:00Z",
      "store_available": true,
      "can_save": true,
      "can_delete_user_override": true,
      "can_test": true
    }
  ]
}
```

`source` 为 `user_store | deployment | none`。`updated_at` 对 deployment value 可为 null。响应顺序由 allowlist 固定，不按 store enumeration。

### 6.2 `PUT /api/keys/{key_name}`

```json
{"value": "secret-input"}
```

成功 `200 KeyStatusItem`；value 不回显。保存只写 OS store，不调用 provider。错误：`422 credential_value_invalid`、`503 credential_store_unavailable`。

### 6.3 `DELETE /api/keys/{key_name}`

无 body。成功 `200 KeyStatusItem`，表达删除 user override 后的 effective 状态。不存在 user override 时幂等返回当前状态；不得删除 deployment value。

### 6.4 `POST /api/keys/{key_name}/test`

无 body；不得自动重试。成功 request 本身固定返回 `200 CredentialTestResponse`：

```json
{
  "key_name": "openai",
  "status": "succeeded",
  "safe_error": null,
  "checked_at": "2026-10-08T01:00:00Z"
}
```

provider 已返回安全失败时仍可用 `status=failed` + stable safe error，HTTP 只表达 endpoint request 是否有效；missing credential 返回 `409 credential_missing`，store/provider transport unavailable 可返回 `503` 且 `retryable` 准确。raw provider body/header 不返回。

## 7. Admin read API

### 7.1 `GET /api/admin/storage`

`200 AdminStorageStatusResponse`：

```json
{
  "history": {"state": "available"},
  "rag_index": {"state": "available"},
  "evidence_cache": {"state": "not_present"},
  "credential_store": {"state": "available"}
}
```

第一版不承诺昂贵的全库 byte/count scan。unknown/permission-limited 使用明确 state，不填伪造的 0。

### 7.2 `GET /api/admin/rag`

返回既有 `RAGIndexHealth` + `RAGProfileHandoffState` 的安全 public projection：active generation、generation status/profile digest/job counts/reader count/timestamps/safe error，以及 current desired profile。不得返回 vectors、canonical text 或 credential。

### 7.3 Operations

```text
GET /api/admin/operations?limit=50&cursor=...
GET /api/admin/operations/{operation_id}
```

列表默认 50、最大 200，按 created_at/operation_id 倒序，opaque cursor；不提供 arbitrary filtering。单项不存在为 `404 admin_operation_not_found`。列表 response：`items[] + next_cursor`。

## 8. History cleanup API

### 8.1 Preview

`POST /api/admin/history-cleanups/preview`

```json
{
  "selection": {
    "kind": "runs",
    "run_ids": ["10000000-0000-4000-8000-000000000001"]
  },
  "cascade_consumers": false
}
```

`selection.kind` 为：

- `runs`：1..500 个唯一 UUID；
- `all_history`：无 run_ids，server 在 preview 时固定选择 fingerprint；超过 500 个 run 或依赖链接时返回 `422 history_cleanup_selection_too_large`，可分批清理。

成功 `200 HistoryCleanupPreviewResponse`：

```json
{
  "requested_run_ids": ["..."],
  "delete_run_ids": ["..."],
  "delete_order": ["..."],
  "dependencies": [],
  "cascade_required": false,
  "cascade_authorized": false,
  "estimated": {
    "runs": 1,
    "sessions": 4,
    "transcript_items": 30,
    "retained_idempotency_tombstones": 1
  },
  "preserved": ["api_keys", "settings", "unit_policy_versions"],
  "confirmation": {}
}
```

若 cascade required 且 request 未授权，preview 仍返回完整影响，但 confirmation 标记不可执行；frontend 必须再次以 `cascade_consumers=true` preview，不能只切换本地 checkbox 后复用旧 token。

### 8.2 Execute

`POST /api/admin/history-cleanups`，required UUID v4 `Idempotency-Key`：

```json
{
  "confirmation_token": "opaque-single-purpose-token",
  "confirmation_phrase": "DELETE 5 RUNS"
}
```

成功 `202 AdminOperationSnapshot`。同 key + same bytes 返回同 operation；不同 bytes 为 `409 idempotency_key_conflict`。dependency/fingerprint 变化为 `409 admin_preview_stale`；活动冲突返回 `409 admin_operation_conflict` 并可附 existing operation snapshot。

operation succeeded result 包含 `RAGCleanupResult` public projection、deleted counts 和 retained tombstone count。

## 9. RAG Admin API

### 9.1 Profile handoff

```text
POST /api/admin/rag/profile-handoffs/preview
POST /api/admin/rag/profile-handoffs
```

preview 无 body，读取 current desired profile，返回现有 handoff state、active/building/retired identities、estimated jobs、可能产生 provider usage 的 warning 和 confirmation。desired 已 active 且无需动作时返回 executable false，不创建 no-op operation。

execute 使用 confirmation token/phrase + required Idempotency-Key，成功 `202 AdminOperationSnapshot`。operation 复用 M8 build/validate/activate/reader-drain facts；旧 active 在新 generation 原子激活前保持可用。

### 9.2 Retry failed jobs

```text
POST /api/admin/rag/generations/{generation_id}/failed-jobs/retry-preview
POST /api/admin/rag/generations/{generation_id}/failed-jobs/retries
```

preview body：

```json
{"safe_error_codes": ["provider_timeout"], "limit": 25}
```

只允许 server 当前报告的 safe codes，limit 1..100，generation 必须 building。execute 使用 token/phrase + Idempotency-Key，返回 `202 operation`。不得 retry completed/excluded/running jobs 或把 filter 扩成 arbitrary SQL。

### 9.3 Delete inactive generation

```text
POST /api/admin/rag/generations/{generation_id}/deletion-preview
POST /api/admin/rag/generations/{generation_id}/deletions
```

preview/execute 都拒绝 active/building generation。retired 必须满足 retention，retired/failed 都必须 reader count 0 且另有可见 active identity符合 M8 operation contract。execute 使用 token + Idempotency-Key，成功 `202 operation`。删除只影响该 generation 的派生 rows，不删除 history/transcript/consumption links/API keys。

## 10. Operation status 与 polling

frontend 收到 `202` 后保存 `operation_id`，只在 Admin 可见时 GET。建议立即、1 s、2 s、最大 5 s 的有界间隔；response 可返回 server `poll_after_ms` hint，client 只在安全范围内采用。

GET 可能看到 `pending → running → terminal`；不得倒退。HTTP disconnect 不取消。terminal 后停止 polling。server restart 后遗留的 operation 必须变为 interrupted/failed 或由底层 durable job reconcile，不能永久 running。

M9 不定义 operation cancel route。需要取消的 generation worker行为必须先在后续 milestone 独立设计，不能把关闭页面当取消。

## 11. Create idempotency tombstone 扩展

删除 run 后，`POST /api/analysis/runs` 使用旧 key：

- same raw request bytes：`410 idempotency_resource_deleted`；
- different raw request bytes：`409 idempotency_key_conflict`；
- 新 UUID v4：正常 `201`。

`410` error 不返回旧 run snapshot/query/result；可返回安全 message 提示主动分析需新 key。frontend 不自动换 key 重发，因为那会在用户未确认时创建新外部调用。

## 12. Configuration snapshot 扩展

`AnalysisConfigurationSnapshot` 增加：

```json
{
  "unit_policy_version": 2,
  "unit_registry_digest": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
}
```

若 session 产生 query-requested unit override，其 typed decision/result 显式记录 parameter、target_unit_id、registry_digest 和 source=`session_override`。create body不增加 arbitrary unit formula，也不直接提交全局 policy。

schema version/backward read 必须保证既有历史可读；旧 record 缺失 M9 field 时 public projection明确 legacy/unknown，不能拿当前 setting补齐。

## 13. Error/status matrix

| status | 典型 code | 语义 |
|---:|---|---|
| 200 | — | read/update/test/preview 成功 |
| 201 | — | 新 unit policy version 创建 |
| 202 | — | async Admin operation 已受理或幂等返回 |
| 400 | `invalid_request` | JSON encoding/content-type 等协议错误 |
| 404 | `credential_not_allowed`, `admin_operation_not_found`, `rag_generation_not_found` | resource/allowlist 不存在 |
| 409 | revision/idempotency/preview/dependency/operation/generation conflict | 当前事实不允许该 mutation |
| 410 | `idempotency_resource_deleted` | create key 对应历史已删除 |
| 422 | `request_validation_failed` | strict DTO/catalog/unit/enum validation |
| 503 | settings/store/provider/database unavailable | 可用性失败，retryable 按实际事实 |

所有错误的 `error.code` 是 stable lowercase token；field violations 使用当前 `validation[]` shape。UI 显示安全 message/request id，不解析英文 message 推导行为。

## 14. OpenAPI 与 contract gate

实现时必须：

- 将所有 route、DTO、enum、error response、headers 和 status 写入 FastAPI OpenAPI；
- `frontend/` 从同一 OpenAPI 生成/校验 TypeScript types；
- raw HTTP tests 验证 content-type、unknown fields/query、origin、no-store、X-Request-ID；
- raw SSE tests继续验证现有两流，不因 static hosting/proxy 被缓冲；
- OpenAPI 中不出现 secret response field、direct tool invocation、evidence-cache cleanup 或旧 WebSocket；
- 同一 MR 同步本文、root frontend contract、runtime schema和 tests。

设计文档完成不等于 route 已实现；只有 production app 注册、OpenAPI 可见且 tests 通过后才能把状态改为 implemented。
