# M2 Analysis Session + SSE API 与排障指南

生成日期：2026-09-08

实现状态（2026-09-08）：已完成 [Implementation TODO 中的 Milestone 0–5](m2_analysis_session_sse_implementation_todo.md#milestone-5http-routes-与统一错误)。正式创建、SSE 订阅与取消路由已接入，支持服务端 request ID、统一 HTTP 错误、SSE replay/keepalive/204 和协作式取消。OpenAPI 已包含 `/health` 与三个 session 操作；接口、streaming、错误与文档 raw frame 均有测试。JSON 日志 formatter/完整 redaction 和正式 SSE server smoke/跨平台 CI 仍由 Milestone 6–7 完成。

本文描述 M2 阶段对前端和调试客户端可见的 HTTP/SSE 契约。普通 HTTP schema 以运行时 OpenAPI 为可执行来源；本文重点补充 OpenAPI 无法完整表达的事件流、回放、keepalive、连接关闭和关联 ID 语义。

M2 是后端内部里程碑，不与现有 WebSocket 前端兼容。最终前端重构应在 M9 按当时的最终 OpenAPI 和事件文档接入。

## 1. Base URL 与限制

本地默认地址：

```text
http://127.0.0.1:8000
```

M2 限制：

- 单进程、单 Uvicorn worker。
- 同一 worker 内允许多个 session 并发。
- session 和 event 只存在于当前进程；服务重启后不可恢复。
- SSE 断开不会取消 session。
- 只有显式 cancel API 会取消 session。
- M2 没有 history/detail API、认证、幂等创建和真实分析结果。

## 2. 关联 ID

| ID | 范围 | 获取位置 | 用途 |
|---|---|---|---|
| `request_id` | 单次 HTTP 请求 | `X-Request-ID`、error body、后端日志 | 定位 POST、SSE 握手或 cancel 请求 |
| `session_id` | 整次分析 | POST response、所有 lifecycle events、后端日志 | 串联完整 session 生命周期 |
| `error_id` | 单次公开错误 | HTTP error 或 `session_failed`、后端异常日志 | 从前端错误回溯 traceback |
| `sequence` | 单 session 公开事件 | SSE `id`、JSON event、后端生命周期日志 | 检查事件遗漏、重复和顺序 |

所有 HTTP response（包括 `/health`、OpenAPI、204 与错误）都带 `X-Request-ID`。每次请求由服务端生成新 UUID，忽略客户端提供的同名 header。后台 workflow 日志关联创建请求的 ID；订阅与 cancel 请求拥有各自 ID，请求结束不会把 ID 留给下一次请求。

排查时优先保留 `session_id`。如果失败响应或事件中有 `error_id`，它是查找具体异常日志的最短路径；HTTP 请求本身的问题再使用 `request_id`。

## 3. 创建 session

### `POST /api/analysis/sessions`

Request：

```json
{
  "query": "Analyze water quality near Austin",
  "time_resolution": "12h"
}
```

字段：

| 字段 | 必填 | 允许值 | 说明 |
|---|---:|---|---|
| `query` | 是 | trim 后非空字符串 | M2 保存但不解析 |
| `time_resolution` | 否 | `6h`、`12h`、`24h` | 省略时使用 `12h`；显式 `null` 非法 |

禁止未知字段。M2 不接受 `auto`、timezone、日期、位置、参数、provider、API key、mode 或 memory mode。

成功响应：`201 Created`

```json
{
  "session_id": "10ef14bd-d0e7-4a70-9728-1fbb4ca7a83c",
  "status": "pending",
  "created_at": "2026-09-08T03:20:00Z",
  "updated_at": "2026-09-08T03:20:00Z",
  "configuration": {
    "analysis_timezone": "America/Chicago",
    "time_resolution": "12h",
    "time_resolution_source": "frontend"
  }
}
```

如果省略 `time_resolution`：

```json
{
  "configuration": {
    "analysis_timezone": "America/Chicago",
    "time_resolution": "12h",
    "time_resolution_source": "default"
  }
}
```

上面的片段只展示 configuration；真实 response 仍包含完整 session snapshot。

Response headers：

```text
Content-Type: application/json
X-Request-ID: <request UUID>
```

注意：POST 固定返回创建时捕获的 `pending` snapshot。后台 workflow 可能已经开始或完成；后续状态以 SSE 为准。

## 4. 订阅 lifecycle events

### `GET /api/analysis/sessions/{session_id}/events`

首次订阅不发送 `Last-Event-ID`。服务端先回放已经存在的 lifecycle events，再等待新事件。

Response：`200 OK`

```text
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache
X-Request-ID: <request UUID>
```

### 4.1 Event envelope

```json
{
  "schema_version": 1,
  "session_id": "10ef14bd-d0e7-4a70-9728-1fbb4ca7a83c",
  "sequence": 1,
  "type": "session_created",
  "occurred_at": "2026-09-08T03:20:00Z",
  "payload": {
    "status": "pending",
    "created_at": "2026-09-08T03:20:00Z",
    "configuration": {
      "analysis_timezone": "America/Chicago",
      "time_resolution": "12h",
      "time_resolution_source": "frontend"
    }
  }
}
```

`payload` 的结构由 event type 决定，不是任意 JSON map。四个具体 event model 通过 `LifecycleEvent` discriminated union 校验，JSON Schema 也按 `type` 表达对应的 payload。所有模型禁止 extra，版本只接受整数 1，sequence 只接受正整数；UUID 输出为标准小写连字符形式，aware datetime 统一输出 UTC，naive datetime 被拒绝。

### 4.2 `session_created`

Raw frame：

```text
id: 1
event: session_created
data: {"schema_version":1,"session_id":"10ef14bd-d0e7-4a70-9728-1fbb4ca7a83c","sequence":1,"occurred_at":"2026-09-08T03:20:00Z","type":"session_created","payload":{"status":"pending","created_at":"2026-09-08T03:20:00Z","configuration":{"analysis_timezone":"America/Chicago","time_resolution":"12h","time_resolution_source":"frontend"}}}

```

### 4.3 `session_completed`

```text
id: 2
event: session_completed
data: {"schema_version":1,"session_id":"10ef14bd-d0e7-4a70-9728-1fbb4ca7a83c","sequence":2,"occurred_at":"2026-09-08T03:20:01Z","type":"session_completed","payload":{"status":"completed","completed_at":"2026-09-08T03:20:01Z"}}

```

M2 不返回 fake 水质结果。最终 result 由 M6/M7 定型。

### 4.4 `session_failed`

```text
id: 2
event: session_failed
data: {"schema_version":1,"session_id":"10ef14bd-d0e7-4a70-9728-1fbb4ca7a83c","sequence":2,"occurred_at":"2026-09-08T03:20:01Z","type":"session_failed","payload":{"status":"failed","failed_at":"2026-09-08T03:20:01Z","error":{"error_id":"0463cd90-6d0f-47e9-9656-b4aa86352c61","code":"workflow_failed","message":"The analysis workflow failed.","failed_stage":"analysis_workflow","retryable":false}}}

```

使用 `error_id` 在后端结构化日志中查找完整异常类型、traceback 和 cause chain。不要期待 SSE message 暴露内部异常文本。

### 4.5 `session_cancelled`

```text
id: 2
event: session_cancelled
data: {"schema_version":1,"session_id":"10ef14bd-d0e7-4a70-9728-1fbb4ca7a83c","sequence":2,"occurred_at":"2026-09-08T03:20:01Z","type":"session_cancelled","payload":{"status":"cancelled","cancelled_at":"2026-09-08T03:20:01Z"}}

```

每个 session 最多出现一个 terminal event：completed、failed、cancelled 三者之一。

### 4.6 Keepalive

15 秒内没有 lifecycle event 时，服务端发送：

```text
: keepalive

```

keepalive 没有 `id`、`event` 或 JSON `data`，不更新客户端 cursor，也不需要展示为日志。它不进入事件存储、session 时间更新或生命周期日志；超时与 lifecycle event 同时就绪时优先发送事件。生产默认 15 秒，测试通过 app factory 的 `keepalive_interval` 注入短间隔，HTTP request、settings 和环境变量没有此开关。

### 4.7 EventSource 示例

```js
const sessionId = "10ef14bd-d0e7-4a70-9728-1fbb4ca7a83c";
const source = new EventSource(`/api/analysis/sessions/${sessionId}/events`);

source.addEventListener("session_created", (event) => {
  const lifecycleEvent = JSON.parse(event.data);
  console.log(lifecycleEvent.session_id, lifecycleEvent.sequence);
});

for (const type of [
  "session_completed",
  "session_failed",
  "session_cancelled",
]) {
  source.addEventListener(type, (event) => {
    const lifecycleEvent = JSON.parse(event.data);
    console.log(lifecycleEvent);
    source.close();
  });
}
```

浏览器 EventSource 会在连接意外中断后自动携带最后收到的 SSE `id` 重连。服务端只补发 sequence 更大的事件。

### 4.8 手工续传

非浏览器客户端可以发送：

```text
Last-Event-ID: 1
```

服务端只返回 sequence 大于 1 的事件。

边界：

- header 缺失：从 sequence 1 开始。
- header 使用 ASCII 十进制非负整数，允许前导零（如 `0001` 等于 `1`）；空字符串、符号、空白、小数或 Unicode 数字返回 HTTP 400 `invalid_event_cursor`。
- 超长合法数字仍按大小判定，不因整数转换长度限制变成格式错误；超过当前最大 sequence 返回 `event_cursor_ahead`。
- cursor 大于服务端当前最大 sequence：HTTP 409 `event_cursor_ahead`。
- session 已终止且 cursor 已消费最后事件：HTTP 204，不再建立 stream。
- session 已终止但 cursor 落后：回放缺失事件后关闭 stream。

GET events 在发送 headers 前完成这些检查：合法订阅返回 `200 text/event-stream`，已消费完终态返回无 body 的 204；非法／越界 cursor 和未知 session 返回 400／409／404 及统一 JSON 错误。response 在正常结束、断线或写入失败时关闭自己的 frame 和 subscription generator，workflow 继续运行。headers 发出后不再追加 HTTP 错误 JSON；workflow 失败仍通过 `session_failed` event 表达。

## 5. 取消 session

### `POST /api/analysis/sessions/{session_id}/cancel`

Request body：无。

JavaScript 示例：

```js
const response = await fetch(
  `/api/analysis/sessions/${sessionId}/cancel`,
  { method: "POST" },
);
const body = await response.json();
```

活动 session 成功响应：`200 OK`

```json
{
  "session_id": "10ef14bd-d0e7-4a70-9728-1fbb4ca7a83c",
  "status": "cancelled",
  "created_at": "2026-09-08T03:20:00Z",
  "updated_at": "2026-09-08T03:20:01Z",
  "configuration": {
    "analysis_timezone": "America/Chicago",
    "time_resolution": "12h",
    "time_resolution_source": "frontend"
  }
}
```

规则：

- pending/running：转换为 cancelled，返回 200。
- cancelled：幂等返回同一终态，仍为 200，不产生第二个 event。
- completed/failed/completed_with_persistence_error：返回 409 `session_already_terminal`，并附当前 session snapshot。
- session 不存在：返回 404 `session_not_found`。

cancel response 表示取消已经被接受，不表示所有在途工作都已停止。后端使用同一 `session_id` 记录稍后的 `workflow_quiesced`。

HTTP cancel 已验证上述时序、幂等和终态保护：cancel 不调用整个 task 的 `Task.cancel()`，晚到返回或异常不再提交 completed/failed，也不进入下游阶段。非 cancelled 终态返回 409 和当前 snapshot；unknown session 返回 404，非法 UUID 返回 422。应用 shutdown 使用独立的服务停止语义，宽限期后可取消并等待在途 task 清理，不生成 `session_cancelled`。

## 6. HTTP error

基础结构：

```json
{
  "error": {
    "error_id": "4a1b720f-0eed-4be0-aaea-8d8300f439ca",
    "request_id": "82ca317a-b20e-45da-9621-8c35a626cbba",
    "session_id": "10ef14bd-d0e7-4a70-9728-1fbb4ca7a83c",
    "code": "session_already_terminal",
    "message": "The session is already completed.",
    "failed_stage": "cancel_session",
    "retryable": false
  },
  "session": {
    "session_id": "10ef14bd-d0e7-4a70-9728-1fbb4ca7a83c",
    "status": "completed",
    "created_at": "2026-09-08T03:20:00Z",
    "updated_at": "2026-09-08T03:20:01Z",
    "configuration": {
      "analysis_timezone": "America/Chicago",
      "time_resolution": "12h",
      "time_resolution_source": "frontend"
    }
  },
  "validation": []
}
```

Error envelope 固定包含 `error`、`session` 和 `validation`；没有 current snapshot 时 `session` 为 null，没有字段校验错误时 `validation` 为空数组。`error.request_id` 必填，`error.session_id` 与 `error.failed_stage` 可为 null。`code` 和非空 `failed_stage` 必须是小写 snake_case，`retryable` 必须是 bool。

字段校验错误示例：

```json
{
  "error": {
    "error_id": "4a1b720f-0eed-4be0-aaea-8d8300f439ca",
    "request_id": "82ca317a-b20e-45da-9621-8c35a626cbba",
    "session_id": null,
    "code": "invalid_request",
    "message": "The request is invalid.",
    "failed_stage": null,
    "retryable": false
  },
  "session": null,
  "validation": [
    {
      "field": ["body", "query"],
      "type": "string_too_short",
      "message": "The query must not be empty."
    }
  ]
}
```

`ValidationViolation.field` 是字符串/整数路径数组；`type` 是稳定 snake_case error type，`message` 是非空安全摘要。不包含原始 `input`、异常 `ctx`、原始 `msg` 或任意 details map。已知字段路径保留，任意未知 JSON key 用 `unknown_field` 代替，避免字段名本身携带敏感输入；不认识的校验 type 归一为 `invalid_value`。HTTP 422 与安全摘要已接入，通用日志 redaction 由 Milestone 6 接入。

错误码：

| HTTP | code | 含义 | 建议处理 |
|---:|---|---|---|
| 400 | `invalid_event_cursor` | Last-Event-ID 格式非法 | 清除本地 cursor 后重新订阅 |
| 404 | `not_found` | HTTP 路由不存在 | 检查 URL；接口不自动重定向尾斜杠 |
| 405 | `method_not_allowed` | HTTP 方法不支持，保留 `Allow` header | 使用文档约定的方法 |
| 404 | `session_not_found` | 当前进程没有该 session | 检查 session_id；服务是否已重启 |
| 409 | `event_cursor_ahead` | cursor 超过服务端最新 sequence | 检查客户端是否混用了 session/cursor |
| 409 | `session_already_terminal` | 不能取消非 cancelled 终态 | 使用响应中的当前 snapshot 更新 UI |
| 422 | `invalid_request` | body/path 校验失败 | 查看 validation，但不要重放相同非法输入 |
| 500 | `internal_error` | 非预期服务错误 | 用 error_id 查询后端日志 |
| 503 | `service_unavailable` | coordinator 已进入关闭阶段，`retryable=true` | 待服务可用后再创建 |
| 其他 HTTP 错误 | `http_error` | 框架 HTTP 错误的安全兜底 | 根据 HTTP status 和 error_id 排查 |

所有 error response 都带 `X-Request-ID`。公开 message 是安全摘要，不包含 traceback 或内部路径。

## 7. 前端状态建议

M2 不公开 `session_started`：

| 后端状态/事件 | 前端显示 |
|---|---|
| POST pending | 分析中 |
| session_created/pending | 分析中 |
| 内部 running | 分析中 |
| session_completed | 已完成 |
| session_failed | 失败 |
| session_cancelled | 已取消 |

前端点击停止必须调用 cancel API；只调用 `EventSource.close()` 不会取消分析。

## 8. 排障 Runbook

### 8.1 POST 没有返回 session

1. 保存 HTTP status、`X-Request-ID` 和 error body。
2. 用 `request_id` 查 `http_request_completed` 或对应 exception log。
3. 如果有 `error_id`，直接查同 ID 的异常日志。
4. validation error 不会记录 raw request body；对照 OpenAPI 检查字段和值。

### 8.2 POST 成功但前端没有事件

1. 确认使用 POST 返回的准确 `session_id`。
2. 不带 cursor 重新请求 events；`session_created` 应当可回放。
3. 检查 SSE response 的 status 与 `Content-Type`。
4. 用 `session_id` 查 `session_created` 和 `lifecycle_event_committed` 日志。
5. 对照日志中的 sequence 与客户端最后记录的 SSE `id`。

### 8.3 SSE 经常断开

1. 断开本身不会取消任务，允许 EventSource 自动重连。
2. 检查是否每 15 秒收到 comment keepalive。
3. 检查代理是否 buffer `text/event-stream`。
4. 检查客户端是否错误地把 keepalive 当作 JSON event。
5. terminal 后服务端主动关闭是正常行为；客户端应关闭 EventSource。

### 8.4 cancel 后仍有后台日志

这是允许的短暂状态：

```text
cancel_accepted -> workflow_quiesced
```

用 session_id 检查：

- 是否只有一个 `session_cancelled` lifecycle event。
- 是否出现 `workflow_output_suppressed`，表示晚到结果没有向前端或下游传播。
- 是否最终出现 `workflow_quiesced`。
- 如果长期没有 quiesced，定位仍在运行的 operation；M2 fake 不应出现此问题。

### 8.5 前端收到 `session_failed`

1. 保存 `session_id`、event `sequence` 和 `error.error_id`。
2. 用 error_id 查后端异常日志，获得 exception type、traceback 和 cause chain。
3. 用 session_id 检查此前状态转换，确认没有 terminal race 或重复 terminal event。
4. 不要求前端解析 traceback；公开 message 只用于用户提示。

### 8.6 服务重启后 session 404

这是 M2 的预期限制。session/event 仅保留在进程内。M3 才提供持久化 history 和启动恢复处理。

## 9. OpenAPI 与文档一致性

实现完成后应检查：

- `/docs` 中 POST/cancel 的 request、response 和 error schema 与本文一致。
- events endpoint 标注 `text/event-stream`，并链接或引用本文的 streaming 语义。
- examples 使用真实模型序列化结果生成或由测试校验字段，避免手写示例漂移。
- API route、schema、本文、smoke client 和测试中没有旧 WebSocket 字段或事件名。
