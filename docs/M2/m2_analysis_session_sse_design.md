# M2 Analysis Session + SSE Shell 详细设计

生成日期：2026-09-08

本文把 `backend_refactor_milestones.md` 中的 M2 展开为可直接实现和验收的技术设计。它服从 `backend_architecture_skeleton.md` 的分层与依赖方向，并承接 M1 已实现的 application factory、settings、`ClockPort`、`EventPublisherPort`、时间粒度类型和跨平台约束。

M2 的目标是跑通 analysis session 的生命周期协议，不实现水文业务推理。`backend_bk/`、`backend_refactor_contract.md` 和现有 WebSocket 前端不是本设计的需求来源、兼容目标或测试 oracle。

对外接口示例与排障说明见 [m2_analysis_session_sse_api.md](m2_analysis_session_sse_api.md)，实现步骤与验收清单见 [m2_analysis_session_sse_implementation_todo.md](m2_analysis_session_sse_implementation_todo.md)。

## 1. 核心决策

- M2 只实现新后端，不读取、适配或修改现有前端，也不提供旧 WebSocket 兼容层。
- M2 严格提供三个 endpoint：创建 session、订阅 SSE、取消 session。单 session 详情与历史 API 属于 M3。
- 创建请求只冻结 `query` 和可选的 `time_resolution`。M2 保存 query 但不理解或解析它。
- session 创建时固化 `analysis_timezone`、实际 `time_resolution` 和 `time_resolution_source`。全局设置后续变化不能修改已有 snapshot。
- session 状态机保留 `pending -> running`，但不新增公开的 `session_started` 事件。前端把所有非终态统一显示为“分析中”。
- 只有显式调用 cancel API 才取消 session。SSE 断开、页面刷新、代理超时或最后一个订阅者离开都不取消任务。
- cancel 立即提交 `cancelled` 终态并返回，不等待 workflow 完全停止。workflow 使用协作式 cancellation token 在阶段边界停止传播。
- cancel 后产生的在途结果不发送给前端、不进入下游 workflow；M3 可以把已经产生的内容写入 cancelled session 的 transcript，不增加 `produced_after_cancel` 一类冗余字段。
- 公开状态转换和对应 lifecycle event 必须原子提交，不能出现终态已生效但 SSE 事件缺失的状态。
- 每个 session 保留进程内完整 lifecycle event 列表，首次订阅可回放，重连通过 `Last-Event-ID` 精确续传。
- SSE 每 15 秒发送无业务语义的 comment keepalive。keepalive 不编号、不回放、不写 transcript，也不逐条写日志。
- 公开事件使用版本化、类型化 Pydantic envelope；payload 不使用任意 `dict[str, Any]`。
- M2 的结构化日志首先服务于 agent 排查。`session_id` 串联整个分析，`request_id` 定位 HTTP 请求，`error_id` 从公开错误回溯后端异常。
- SSE、结构化 lifecycle 日志和未来 transcript 复用同一个 `LifecycleEvent`；不建立第二套事件模型。
- M2 使用单进程、单 Uvicorn worker，但允许同一 worker 内多个 session 并发运行。
- session、事件和 task handle 只保存在内存中，直到进程退出。M2 不实现 TTL、LRU、容量上限或后台清理任务。
- M2 不支持创建幂等键；每次成功 POST 都产生新 session。持久化幂等创建属于 M3。
- M2 不新增生产或开发依赖，继续使用 FastAPI/Starlette、Pydantic、`asyncio`、`logging`、`uuid`、pytest 和 httpx。
- M1-M9 是内部演进阶段，不要求 milestone 间对外兼容；最终只以 M9 完成后的统一协议为准。

## 2. 范围

### 2.1 M2 包含

- `POST /api/analysis/sessions`
- `GET /api/analysis/sessions/{session_id}/events`
- `POST /api/analysis/sessions/{session_id}/cancel`
- session 状态机与原子终态竞争
- 创建时的 timezone/resolution snapshot
- 进程内 session runtime 与 lifecycle event replay
- SSE framing、keepalive、断线续传和多订阅者
- 协作式 cancel 与 workflow quiesced 跟踪
- 可注入的 fake workflow，可测试 completed、failed 和 cancelled
- 统一 HTTP error envelope
- 面向 agent 排查的结构化 JSON 日志和关联 ID
- application lifespan 中的 task 收敛
- API/SSE 文档、in-process 测试、真实 Uvicorn smoke 和跨平台 CI

### 2.2 M2 不包含

- 现有前端修改、临时调试页面或旧 WebSocket 兼容
- `GET /api/analysis/sessions` 和 `GET /api/analysis/sessions/{session_id}`
- transcript schema、数据库、事件跨重启恢复或历史持久化
- query understanding、日期 mapper、位置、参数、mode 或 memory mode 解析
- `auto` 时间粒度决策
- topology、evidence、LLM、Agent 或真实分析结果
- 创建幂等键
- 多进程或多 worker 的共享 session、跨进程 cancel 和 pub/sub
- 并发上限、排队、优先级或资源配额
- TTL、LRU、容量上限和 admin cleanup
- 认证、授权、限流或分布式 tracing
- 复杂 graceful drain、暂停/恢复或 session 自动重放

## 3. 建议目录与职责

目录以职责边界为准，不要求为单个小类拆文件。实现时优先保持模块少而清晰：

```text
backend/app/
  main.py
  api/
    http/
      analysis_sessions.py
      errors.py
      request_context.py
    schemas/
      analysis_sessions.py
    sse/
      encoder.py
      response.py
  application/
    correlation.py
    analysis_sessions/
      models.py
      coordinator.py
      runtime.py
      workflow.py
  infrastructure/
    observability/
      json_logging.py
      redaction.py
    runtime/
      system_clock.py
```

职责：

- `api/http/analysis_sessions.py`
  - HTTP 路由、header/path/body 校验、状态码和 response mapping。
  - 不直接修改 session 状态，不直接操作 workflow task。
- `api/http/errors.py`
  - application/HTTP/validation 错误映射、固定安全摘要、error ID 和统一 envelope；不复制原始异常文本或校验 input。
- `api/http/request_context.py`
  - 纯 ASGI request ID、response header 和请求上下文生命周期，不缓冲 SSE；未开始发送时接管未处理异常。
- `application/correlation.py`
  - request ID 的 ContextVar，供 HTTP 边界绑定以及 application 日志读取。
- `api/schemas/analysis_sessions.py`
  - 对外 request、success response 和 error response。
  - API schema 与 application model 分离，但只做必要映射。
- `api/sse/encoder.py`
  - 把一个 `LifecycleEvent` 编码成标准 SSE frame。
  - 编码函数保持纯函数并接受协议级单元测试。
- `api/sse/response.py`
  - 在开始发送前完成 application 订阅校验，将已消费完的终态映射为 204，其余订阅编码为 StreamingResponse。
  - response 拥有本次订阅及 frame generator，结束、断线或发送失败时关闭两者；统一错误 envelope 和正式路由由 `api/http` 接入。
- `application/analysis_sessions/models.py`
  - `SessionStatus`、configuration snapshot、session snapshot、lifecycle event、公开错误等稳定。
- `application/analysis_sessions/coordinator.py`
  - 唯一状态转换入口。
  - 创建 session、启动 workflow、完成、失败、取消和 task quiesced。
- `application/analysis_sessions/runtime.py`
  - 同时保存进程内 session、公开事件列表和订阅等待条件。
  - 在单 session 临界区中原子提交 snapshot 与 lifecycle event。
  - 只由 coordinator 使用；API 通过 coordinator 的 application-facing 方法创建、取消和订阅。
- `application/analysis_sessions/workflow.py`
  - M2 最小 workflow Protocol、cancellation context 和 fake 实现。
  - 不建立通用 workflow framework。
- `system_clock.py`
  - `ClockPort` 的 UTC system clock 实现。
- `json_logging.py`
  - WaterAgents 自有日志的单行 JSON formatter、关联字段、异常诊断和容错输出 handler。
- `redaction.py`
  - JSON-like 值的递归脱敏，以及凭据文本和已知 query 的诊断脱敏。

`app.main.create_app` 继续是 composition root。为了测试，可以增加少量 keyword-only 注入点或一个小型 frozen dependencies 对象；不得引入 service locator 或通用 DI framework。现有 `create_app(settings=None)` 调用保持可用。

## 4. Session 模型

### 4.1 `SessionStatus`

稳定枚举：

```text
pending
running
completed
failed
cancelled
completed_with_persistence_error
```

`completed_with_persistence_error` 在 M2 中只冻结名称，M2 不实际产生该状态。

### 4.2 `AnalysisConfigurationSnapshot`

字段：

```text
analysis_timezone: str
time_resolution: TimeResolution
time_resolution_source: TimeResolutionSource
```

约束：

- `analysis_timezone` 从 `AppSettings.analysis_timezone` 复制，不保留 settings 对象引用。
- `time_resolution` 必须是 M1 已定义的 `6h / 12h / 24h`。
- 请求显式提供有效 resolution 时 source 为 `frontend`。
- 请求省略 resolution 时使用 `DEFAULT_TIME_RESOLUTION`，source 为 `default`。
- 显式 JSON `null` 不等同于省略，应由请求校验拒绝。
- M2 不产生 source=`auto`；M4 定义自动选择规则后才使用。
- `CreateAnalysisSessionRequest.to_configuration_snapshot(analysis_timezone)` 根据 `model_fields_set` 固化 source，并校验 IANA timezone；不持有 settings 引用。

### 4.3 `AnalysisSessionSnapshot`

对 API 和日志可安全暴露的 immutable snapshot：

```text
session_id: UUID
status: SessionStatus
created_at: aware UTC datetime
updated_at: aware UTC datetime
configuration: AnalysisConfigurationSnapshot
```

约束：

- `created_at` 创建后不变。
- 每次成功状态转换更新 `updated_at`。
- 同一次状态转换只调用一次 `ClockPort.now()`；snapshot 时间和对应 lifecycle event 时间完全相同。
- API 中 UUID 序列化为标准小写连字符字符串，时间使用 UTC ISO 8601。
- snapshot 不回显 query。

### 4.4 内部 session 与 runtime

内部 session 在公开 snapshot 之外保存：

```text
query
last_public_sequence
last_error, if any
```

每个 `SessionRuntime` 保存可变的运行期资源：

```text
current immutable session state
ordered lifecycle events
asyncio.Condition or equivalent per-session synchronization
explicit cancel event
stop event/token shared by user cancel and service shutdown
workflow registration gate
workflow task handle
workflow quiesced event/flag
```

可变 runtime 不使用 Pydantic，也不得通过 API 序列化。不同 session 不共享锁、事件列表、取消信号或 task handle。

Application 入口为 `SessionCoordinator`：`create_session(query, configuration)` 创建 pending snapshot/created event，在同一 map 临界区登记 workflow task handle 和 session，再返回捕获的 pending snapshot；`mark_running`、`complete_session`、`fail_session`、`cancel_session` 是状态转换入口，`get_snapshot` 返回 immutable snapshot。`subscribe(session_id, after_sequence=...)` 在返回 async generator 前完成 lookup、cursor 与终态校验；已消费完终态返回 `None`，否则在同一 condition 协议内读取已有事件和等待通知。generator 的 `None` item 表示 keepalive，LifecycleEvent item 复用原事件对象，终态事件发完后结束。纯 SSE encoder、response factory、正式 HTTP 路由及统一错误映射已接入。API 不获取 runtime、锁、task 或可变 event list。

Lookup 接受 UUID 或 UUID 字符串；非法 UUID 产生 Pydantic validation error，合法但不存在的 UUID 产生 `SessionNotFoundError`。非法状态转换产生带当前 immutable `session` 和目标状态的 `InvalidSessionTransitionError`；终态竞争的失败方通过该 snapshot 得到胜出终态。重复 cancel 已 cancelled 的 session 返回同一 snapshot，不读取时钟、不追加事件。HTTP handler 将这些错误映射到统一 envelope，非法 path UUID 在路由校验时返回 422。

## 5. 创建请求与响应

### 5.1 `CreateAnalysisSessionRequest`

```text
query: non-empty trimmed string
time_resolution: TimeResolution = 12h
```

规则：

- 禁止未知字段。
- query 是稳定的用户意图入口，但 M2 不解析其业务含义。
- request model 需要区分 time_resolution 是省略还是显式提供，以产生正确 source。
- 原始请求重序列化使用 `model_dump_json(exclude_unset=True)` 保留字段是否提供；规范化后的 configuration snapshot 显式保存 resolution/source，可直接 JSON round-trip。
- M2 不接受 `auto`、timezone override、日期、位置、参数、provider、API key、mode 或 memory mode。
- M4 可以直接修订该内部阶段契约，不需要为 M2 客户端维护兼容层。

### 5.2 创建顺序

```text
1. 校验请求
2. 生成 request_id 和 session_id
3. 从 settings/request 构造 configuration snapshot
4. 用同一个 UTC timestamp 构造 pending session 与 session_created event
5. 在同一 map 临界区创建 task、登记 handle，并保存 session + sequence 1 event
6. 解锁并记录 created 后释放 workflow registration gate
7. 返回创建时捕获的 pending snapshot
```

注册 gate 保证 eager task factory 也只能在 task/session 登记完成、map 解锁后运行 workflow；task 创建失败不会留下可查询但没有 owner 的 session。HTTP 创建入口调用该 application 顺序，返回 201 和创建时捕获的 pending snapshot；request ID 由外层 ASGI middleware 生成。

即使后台 task 在 HTTP response 完成序列化前已经进入 running 或终态，POST 仍固定返回创建时的 `pending` snapshot。客户端通过 SSE 获得后续生命周期。

### 5.3 HTTP 语义

- 成功状态码：`201 Created`。
- 每次成功 POST 创建新的 UUID session；M2 不读取 `Idempotency-Key`。
- 响应添加 `X-Request-ID`。
- 响应不回显 query，不返回 fake result。

## 6. 状态机

### 6.1 M2 合法转换

```text
pending -> running
pending -> cancelled
running -> completed
running -> failed
running -> cancelled
```

M3 增加：

```text
running -> completed_with_persistence_error
```

终态：

```text
completed
failed
cancelled
completed_with_persistence_error
```

终态一旦提交，不得被任何晚到的 workflow 输出、异常、cancel 或 persistence callback 覆盖。

### 6.2 原子提交

公开转换必须在同一个 session 临界区中完成：

```text
validate current status
derive next immutable snapshot
allocate next public sequence
create typed LifecycleEvent
commit snapshot + event
notify all subscribers
```

锁内不执行网络、文件、数据库、LLM 或任意不受控 callback。结构化日志在成功提交后复用同一事件对象记录；日志失败不得回滚状态或事件。

`pending -> running` 没有公开 lifecycle event，只原子更新内部 snapshot 并记录一次结构化状态转换日志。

Coordinator 复用同步、无外部 I/O 的 `ClockPort.now()`，在通过当前状态校验后读取一次时间并构造完整 snapshot/event，再提交变更。condition 内不调用 logger；解锁后向标准库 logger 传入 committed `LifecycleEvent`、`CorrelationContext`、`from_status/to_status` 和该次转换 timestamp。JSON formatter 直接序列化同一事件，logger/handler 异常不改变已提交状态。Task wrapper 在运行前提交 internal running；正常返回经过停止信号检查并通过同一状态转换入口提交 completed。取消或关闭获胜后，晚到返回和异常只记录 suppression，不覆盖终态。

### 6.3 completion/cancel 竞争

- completion、failure 和 cancel 使用同一状态转换入口和同一把 session 锁。
- 第一个成功提交终态的操作获胜。
- 失败方读取已经存在的终态，不再发布第二个 terminal event。
- 因此每个 session 最多有一个 terminal lifecycle event。

## 7. Lifecycle Event

### 7.1 Envelope

所有公开事件使用同一个版本化 envelope：

```text
schema_version: 1
session_id: UUID
sequence: positive integer
type: LifecycleEventType
occurred_at: aware UTC datetime
payload: discriminated typed payload
```

M2 event types：

```text
session_created
session_completed
session_failed
session_cancelled
```

不增加 `session_started`。`type` 和 payload 的具体模型必须匹配，不能构造交叉组合。

实现使用 `SessionCreatedEvent`、`SessionCompletedEvent`、`SessionFailedEvent`、`SessionCancelledEvent` 四种 immutable envelope，共享基础字段；`LifecycleEvent` 是以 `type` 为 discriminator 的 union，可由 `TypeAdapter(LifecycleEvent)` 校验和生成 JSON Schema。每个分支固定自己的 type、payload 类型及 payload status，避免仅靠运行时 validator 校验而让 JSON Schema 接受错配。`schema_version` 只接受整数 1，sequence 只接受正整数（拒绝 bool、浮点数和数字字符串），created 的 sequence 固定为 1。

### 7.2 Payload

`session_created`：

```text
status = pending
created_at
configuration snapshot
```

`session_completed`：

```text
status = completed
completed_at
```

`session_failed`：

```text
status = failed
failed_at
error: PublicError
```

`session_cancelled`：

```text
status = cancelled
cancelled_at
```

M2 的 completed event 不携带 placeholder、fake 水质结果或空 evidence。M6/M7 在真实结果契约出现后直接修订 completed payload。

### 7.3 Sequence

- sequence 在单个 session 内从 1 开始严格递增。
- `session_created` 固定为 sequence 1。
- keepalive 不占 sequence。
- sequence 不要求跨 session 全局唯一；`session_id + sequence` 是完整身份。
- event append 顺序就是 replay 顺序，不按客户端接收时间重新排序。

## 8. SSE 协议

### 8.1 Frame

一个 lifecycle event 编码为：

```text
id: <sequence>
event: <type>
data: <compact JSON LifecycleEvent>

```

说明：

- SSE `id` 与 JSON `sequence` 相同。
- SSE `event` 与 JSON `type` 相同。
- 这些字段有意重复，使浏览器 EventSource、非浏览器客户端、日志和未来 transcript 都能直接使用统一 DTO。
- data 使用单行 UTF-8 JSON；换行等内容由 JSON escaping 处理。
- 每个 frame 以空行结束。

### 8.2 Response headers

至少包含：

```text
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache
X-Request-ID: <request UUID>
```

实现应避免对 event stream 应用会阻止及时 flush 的响应 buffering 或整体压缩。不得依赖某个反向代理才能正确工作。

### 8.3 首次订阅与重连

- 没有 `Last-Event-ID` 时从 sequence 1 回放当前已有的全部事件，然后等待新事件。
- 有合法 `Last-Event-ID=N` 时只发送 sequence 大于 N 的事件。
- 首次回放与注册等待必须在同一个同步协议中完成，不能在两者之间遗漏刚发布的事件。
- `Last-Event-ID` 使用 ASCII 十进制非负整数，允许前导零；缺失表示 0，空字符串、符号、空白、小数和 Unicode 数字均为非法格式。非整数、负数或格式非法的 cursor 在开始 stream 前返回 HTTP 400。
- application 的 `subscribe(session_id, after_sequence=...)` 同时接受缺省值、严格非负 int 或上述数字字符串。在同一个 condition 临界区校验最大 sequence 与终态；字符串先去前导零并与最大 sequence 比较，再转换为 int，避免超长合法整数被误判为格式错误。
- `InvalidEventCursorError`、`EventCursorAheadError`、`SessionNotFoundError` 由 HTTP handler 分别映射 400/409/404；异常不保存原始 cursor 内容。已消费完终态返回 `None`，response factory 将其映射为无 body 的 204。
- cursor 大于当前最大 sequence 返回 HTTP 409，避免静默等待一个不可能的历史位置。
- session 不存在返回 HTTP 404。
- 如果 session 已终止且客户端 cursor 已消费最后一个 terminal event，返回 HTTP 204，阻止 EventSource 无意义地自动重连。
- 如果 session 已终止但客户端仍缺事件，先回放缺失事件，再关闭 stream。

### 8.4 Keepalive

- 当 15 秒内没有业务事件时发送 `: keepalive\n\n`。
- coordinator 与 `create_app` 接受 keyword-only `keepalive_interval`，默认 15 秒，只允许有限正数；不增加环境变量或 request/settings 开关。
- iterator 在 session condition 内使用 `asyncio.timeout` 等待事件，超时后持锁再次检查事件与终态；若两者同时就绪，优先发送 lifecycle event。空闲时 yield `None` 作为仅传输层可见的 keepalive 信号，由 encoder 层映射为 comment，不能作为 LifecycleEvent 保存或记录。
- 等待、计时和取消都在当前订阅 task 内完成，不为每次心跳创建后台 task。订阅者停止读取时不启动独立定时器。
- keepalive 无 event ID 和 JSON data。
- keepalive 不进入 replay buffer、structured lifecycle log 或 transcript。

### 8.5 订阅者与断开

- 同一 session 允许多个订阅者。
- 每个订阅者维护自己的 cursor，但共享同一 immutable event list。
- 慢订阅者从共享列表按 cursor 读取，不为每个订阅者复制长期事件队列。
- 订阅者断开只结束该 generator，不改变 session 状态，也不影响其他订阅者。
- 客户端收到 terminal event 后应主动关闭 EventSource；服务端在发送完 terminal event 后也结束 stream。

## 9. Cancel

### 9.1 HTTP 行为

`POST /api/analysis/sessions/{session_id}/cancel` 不需要 request body。

- pending/running：原子转换为 cancelled，发布 `session_cancelled`，设置 cancellation token，返回 HTTP 200 和 cancelled snapshot。
- cancelled：幂等返回 HTTP 200 和当前 snapshot，不发布重复事件。
- completed/failed/completed_with_persistence_error：返回 HTTP 409；错误响应同时附带当前 session snapshot。
- session 不存在：返回 HTTP 404。
- path 不是合法 UUID：返回标准化 HTTP 422 validation error。

### 9.2 协作式取消

cancel 接受和 workflow 完全停止是两个时间点：

```text
cancel_accepted
  -> session terminal event visible to frontend
  -> cancellation token visible to workflow
  -> in-flight operation returns or is best-effort interrupted
  -> workflow checks token and stops downstream propagation
  -> workflow_quiesced
```

- cancel endpoint 不等待 `workflow_quiesced`。
- 用户 cancel 不立即对整个 task 调用无条件 `Task.cancel()`；否则会跳过必要清理，也无法接收已经完成的在途结果。
- workflow 在开始前和每个阶段边界检查 token。
- 能安全中止的 adapter 可响应 token；无法强杀的在途调用允许自然返回。
- cancel 获胜后，晚到的返回值不得触发下游工作、成功状态或前端结果事件。
- M3 可以把已经产生的内容追加到 cancelled session transcript。transcript item 不增加 after-cancel 布尔字段；所属 session 的终态就是解释上下文。
- workflow `finally` 必须标记 quiesced 并写结构化日志。

## 10. Fake Workflow

M2 定义一个最小、可替换的 async workflow seam：

```text
AnalysisWorkflow.run(context: WorkflowContext) -> None
WorkflowContext(session: AnalysisSessionSnapshot, query: str, cancellation: CancellationToken)
CancellationToken.is_requested() / checkpoint() / await wait()
```

约束：

- 默认 fake 只完成生命周期，不生成 topology、evidence、水质或 attribution。
- fake completed 正常返回，由 coordinator 提交 completed。
- fake failed 抛出 `WorkflowFailed`；它与非预期 `Exception` 都由 coordinator 映射到唯一 `session_failed`。公开错误固定为 `workflow_failed`、安全 message、`analysis_workflow` stage 和 `retryable=false`，不采用异常原文。
- context 使用 frozen dataclass，复用已提交的 running snapshot；query 仅供 workflow 内部使用，不进入 repr 或公开 snapshot。
- `checkpoint()` 在停止信号已设置时抛出 `WorkflowStopped`；wrapper 在开始前和返回后检查，workflow 在每个下游阶段开始前检查。用户 cancel 和 shutdown 均设置内部 stop signal，只有用户 cancel 设置 explicit cancel event 并提交 cancelled。
- wrapper 的 `finally` 标记 `workflow_quiesced`；done callback 消费 task exception，并覆盖 task 首次执行前即被取消、未进入 finally 的收敛路径。
- controlled completed/failed、gate-based cancellable 和晚到返回 fake 定义在 `backend/tests/application/test_session_workflow.py`；生产模块只保留默认 lifecycle fake。
- fake cancelled 通过 gate 和 cancellation token 验证取消与 quiesced。
- 测试 fake 使用 `asyncio.Event` 等 gate 控制时序，不依赖真实 sleep。
- fake outcome 通过 `create_app` 的测试注入控制，不出现在生产 HTTP request、环境变量或用户设置中。
- M4 可以直接替换或修订该 seam，不保留 fake 兼容层。

## 11. 错误契约

### 11.1 `PublicError`

可进入 SSE 或 HTTP 的公开错误字段：

```text
error_id: UUID
code: stable snake_case string
message: safe human-readable message
failed_stage: safe stable stage name or null
retryable: bool
```

公开 message 不直接使用原始异常字符串，不包含 traceback、内部路径、请求 headers、query 或 secret。

### 11.2 HTTP error envelope

所有 WaterAgents HTTP error 使用：

```text
error: PublicError plus request_id and optional session_id
session: current AnalysisSessionSnapshot or null
validation: typed field violations or empty
```

- `request_id` 每个 HTTP 请求都有。
- `session` 只在例如 cancel terminal conflict 时返回，复用正常 snapshot，不使用自由形态 details map。
- validation error 只给出 field path、stable error type 和 safe message，不回显原始 input。
- 所有 response（包括 health、OpenAPI、SSE 握手、204 和错误）带服务端生成的 `X-Request-ID`；客户端同名 header 不参与 ID 选择。
- 纯 ASGI request context middleware 将 UUID 放入 request state，并通过 ContextVar 供 application 日志使用；结束时恢复上下文。后台 workflow 保留创建请求的 ID，cancel 和订阅请求使用各自 ID。
- request validation 仅保留安全 field path、已知错误 type 和固定摘要；未知 JSON key 映射为 `unknown_field`，不复制 `input`、`ctx`、原始 `msg` 或未知错误 type。
- 未知路由返回 404 `not_found`，方法不支持返回 405 `method_not_allowed` 并保留 `Allow`；禁用自动尾斜杠重定向，使错误统一进入 envelope。已关闭的 coordinator 返回 503 `service_unavailable`，可重试。
- middleware 在 headers 发出前将未处理异常映射为安全 500；headers 发出后不追加 HTTP JSON，也不更改 session 状态。

API schema 使用 `AnalysisSessionResponse` 继承公开 snapshot 字段，`HttpPublicError` 继承 `PublicError` 并增加必填 UUID `request_id` 与可空 UUID `session_id`。`AnalysisSessionErrorResponse` 固定包含 `error`、`session`、`validation`；不适用时后两项分别为 null 和空数组。`ValidationViolation` 的字段为 `field`（字符串/整数路径数组）、`type`（stable snake_case error type）和 `message`（非空安全摘要），不包含原始 `input`、异常 `ctx` 或任意 details map。

所有模型禁止 extra；公开错误的 `code` 与非空 `failed_stage` 使用小写 snake_case，`retryable` 只接受 bool。最小 `CorrelationContext` 包含可空 UUID `request_id/session_id/error_id` 和可空正整数 `event_sequence`。模型只校验结构；HTTP handler 从固定安全 message 构造公开错误，去除 validation input/ctx/原始 msg 并隐藏未知 field key。日志输出边界统一执行 redaction，不能以字符串类型校验代替脱敏。

M2 稳定错误码至少包括：

```text
invalid_request
invalid_event_cursor
event_cursor_ahead
session_not_found
session_already_terminal
workflow_failed
internal_error
not_found
method_not_allowed
service_unavailable
http_error
```

### 11.3 SSE 中途失败

- stream headers 发送前的错误使用普通 HTTP error response。
- stream 建立后，workflow 失败通过 `session_failed` event 表达。
- 单个订阅连接写失败只结束该订阅，不把 session 标记 failed。
- runtime/coordinator 的 invariant violation 记录 error_id 和 traceback；如果仍可安全提交 session failure，则提交一次 `session_failed`，否则至少保证不发布伪造的 completed。

## 12. 三层可观察性

### 12.1 复用原则

三层职责不同，但不复制业务模型：

```text
LifecycleEvent
  -> SSE public frame
  -> structured lifecycle log
  -> M3 transcript projection
```

共享小对象：

- `CorrelationContext`：`request_id/session_id/error_id/event_sequence`
- `LifecycleEvent`：生命周期事实
- `PublicError`：可公开错误

不实现通用 event bus、span framework 或多套事件 mapper。

### 12.2 SSE

- 面向前端，稳定、精简、可回放。
- 只包含安全字段。
- 不包含 traceback 或内部对象 repr。

### 12.3 Structured backend log

WaterAgents 自有应用日志输出单行 JSON，至少支持：

```text
timestamp
level
event_name
message
request_id
session_id
event_sequence
error_id
component
operation
from_status
to_status
method
route
status_code
duration_ms
exception_type
exception_message
stack_trace
```

字段按事件需要出现，不强制用 null 填满每一行。关键日志：

```text
http_request_completed
http_request_failed
http_stream_failed
session_status_changed
lifecycle_event_committed
cancel_accepted
workflow_output_suppressed
workflow_quiesced
session_failed
service_shutdown
workflow_task_error
```

要求：

- 状态名、event name 和错误码使用稳定机器可读值。
- traceback 和 cause chain 只存在于后端错误日志。
- 不把 raw query、完整 request body 或完整 headers 写入普通日志。M3 transcript 才是 user request 的事实源。
- 不逐条记录 keepalive。
- 日志失败不得改变 session 结果。

App lifespan 用 `AppSettings.log_level` 配置 `wateragents` logger，向 stderr 输出单行 JSON。非 ASCII 字符使用 JSON Unicode 转义，避免 Windows 等非 UTF-8 控制台损坏 JSON。重复启动复用一个自有 handler，不向 root logger 传播，不改 Uvicorn/第三方 logger。配置为进程级；正常运行只有一个 app/worker，同进程测试中的多个 app 共享最近一次 lifespan 设置的日志级别。导入 app 或调用 factory 不安装 handler。

`timestamp/level/event_name/message/component` 稳定存在；`component` 是 logger 名称。`CorrelationContext` 展平为非空 ID 字段，状态转换包含 `from_status/to_status`；创建时 `from_status=null`。公开事件统一记录为 `lifecycle_event_committed`，其 `lifecycle_event` 字段直接序列化 committed `LifecycleEvent`，`event_sequence` 与 SSE sequence 相同；不额外写一条 `session_created` 日志。状态转换使用同一次 timestamp，独立 activity/HTTP 日志通过 `ClockPort` 读取发生时间。

HTTP completion 记录 `method/route/status_code/duration_ms`；route 为模板，不含实际 URL、path 参数或 query string，未匹配时省略。耗时为 `ClockPort` 的结束 UTC 时间减开始 UTC 时间，单位毫秒，时钟回拨时截断为 0；SSE 包含整个订阅持续时间。时钟故障时不影响请求，允许缺少 duration 或丢弃该条日志。

`session_failed`、晚到异常的 `workflow_output_suppressed`、`workflow_task_error` 及未处理 HTTP/stream 异常记录 ERROR 级诊断：`exception_type/exception_message/stack_trace`。Stack trace 保留文件、行号、函数、异常消息以及标准 cause/context chain 和 exception group，省略源码行和局部变量。失败 session 使用同一公开 error ID；cancel/shutdown 后的晚到异常不生成新公开错误。HTTP validation 和已知 HTTPException 只记安全公开错误与异常类型，不格式化可能含 request body/detail 的原始异常。Headers 已发出的异常记 `http_stream_failed`，随后仍由传输层收尾，不追加错误 JSON，也不修改 session。

Formatter 只选择上述字段及已有 `public_error/lifecycle_event/original_status/outcome`，不序列化整个 LogRecord。格式化、序列化或 redaction 失败时只输出固定安全 JSON：`event_name=log_format_failed`、`level=ERROR`、`timestamp=null` 和固定 message，不尝试 repr 原始对象。Handler 写入失败时丢弃该条日志，不调用会向 stderr 打印原始 record 的默认 logging error handler。调用点继续隔离 logger 异常，不回滚业务结果。

### 12.4 Transcript

M2 不实现 transcript。M3 使用相同 lifecycle event 构造可审计 item，并记录 user request、取消上下文和业务错误。HTTP access log、keepalive 和纯传输异常不默认进入 transcript。

### 12.5 Redaction

- 不记录原始 Authorization、Cookie、Set-Cookie、API key 或 credential headers。
- 对结构化 payload 按大小写不敏感的 secret key allow/deny policy 递归脱敏。
- `redact` 返回新 JSON-like 结构，不修改输入；含 authorization、cookie、API/access key、token、secret、password/passwd、credential、private key 的 key，以及 query/request_body/headers，值统一替换为 `[REDACTED]`。保留安全的键、数组顺序与标量。
- 诊断文本处理上述具名凭据赋值、Authorization/Cookie header、Bearer/Basic 和 URL 用户凭据。Workflow 使用内部 query、HTTP create 使用已校验 query 作为本条异常的私有脱敏值，同时屏蔽其原文、repr 和 JSON 转义形式。私有值只在格式化时使用，不输出到 JSON；不读取或缓存原始 request body/完整 headers。
- 公开错误永远从安全 message 构造，不把异常字符串直接透传。
- M2 没有 API key 输入；redaction helper 是后续 settings/transcript 可复用的最小能力，不扩展成通用 DLP 系统。

## 13. 并发与生命周期

### 13.1 单 worker 契约

- 只支持单进程、单 Uvicorn worker。
- 文档和启动命令不得建议 `--workers > 1`。
- 多 worker 下 POST、SSE 和 cancel 可能命中不同进程，因此明确标为不支持，而不是依赖 sticky session 偶然工作。
- M2 不增加数据库、Redis、消息队列或共享 pub/sub。

### 13.2 多 session

- 单 worker 内允许多个 session 并发。
- 每个 session 使用独立 runtime synchronization。
- runtime 的全局锁只用于 session map 的短时查找/插入，不能覆盖整个 workflow。
- cancel、failure、subscriber disconnect 和 event sequence 必须跨 session 隔离。
- M2 不实现最大并发数或排队策略。

### 13.3 内存保留

- pending、running 和所有 terminal session 都保留到进程退出。
- 终态 session 在同一进程生命周期中始终可通过 SSE replay。
- 不运行 TTL/LRU/background cleanup。
- 文档明确长期运行会有内存增长，M2 不是生产部署形态。

### 13.4 Application shutdown

- `create_app` 的 lifespan 启动时创建 coordinator，保存到 `application.state.session_coordinator`；每次 lifespan 使用独立的内存 runtime，模块导入与 app factory 不创建后台 task。
- coordinator 持有全部 session 的 workflow task handle。shutdown 关闭创建入口，并在 map 锁内捕获仍活动 task 的 session 原状态，向它们发出 stop signal；包括已 cancelled 但尚未 quiesced 的 task。
- 默认等待 1 秒协作式宽限期，再对尚未结束的 task 调用 `Task.cancel()` 并 await 全部任务完成清理；keyword-only `shutdown_grace_period` 可在组合时注入有限非负秒数，测试通过 gate 和零宽限期验证。此宽限期不承诺能强制终止忽略 cancellation 的任意 Python 代码。
- 关闭过程幂等并串行化；关闭后创建抛出内部 `ServiceShuttingDownError`，workflow 不能再提交 running/completed/failed。
- shutdown 不是用户 cancel，不发布 `session_cancelled`。
- M2 记录结构化 `service_shutdown`，包含 `session_id`、`original_status` 以及 `outcome=quiesced/task_cancelled`；保留 session 的原 snapshot 和 event list。
- 不为内存数据增加关闭时持久化。
- M3 在启动恢复时把持久化中遗留的 pending/running session 归档为 failed，错误码 `service_interrupted`，不自动恢复执行。

## 14. Composition 与依赖注入

M2 默认 composition：

```text
AppSettings
SystemClock implements ClockPort
uuid4 ID factory
application-owned InMemorySessionRuntime
SessionCoordinator
DefaultFakeWorkflow
analysis session router
```

测试需要替换：

```text
ClockPort
ID factory
fake workflow
keepalive interval
```

可以通过少量 keyword-only 参数或一个小型 frozen `ApplicationDependencies` 完成。不得为 M2 引入容器扫描、全局 singleton、动态 service registry 或 provider framework。

`EventPublisherPort[LifecycleEvent]` 保留为未来外部 event sink 的窄扩展点。M2 的权威 SSE event list 属于 application-owned in-memory runtime 的原子 commit，SSE 正确性不依赖外部 publisher 成功。等 M3 出现真实持久化能力时，再基于实际读写语义定义 store port；M2 不为单一内存实现预造 repository abstraction。

## 15. API 文档原则

- Pydantic schema 和 FastAPI route metadata 是普通 HTTP schema 的可执行来源。
- `docs/M2/m2_analysis_session_sse_api.md` 补充 OpenAPI 无法完整表达的 streaming、replay、keepalive、terminal close 和关联 ID 语义。
- 文档提供 raw SSE frame、JavaScript EventSource 示例、cancel 示例和错误排查路径，但不修改现有前端。
- 文档明确 M2 是阶段性协议；前端重构应以 M9 时的最终 OpenAPI 和事件文档为准。
- API schema、Markdown 示例和测试中的字段名必须做一致性 sweep。

## 16. 测试设计

### 16.1 状态机

- 所有合法转换成功。
- 所有非法转换失败且不产生事件。
- terminal 状态不可覆盖。
- cancel/completed、cancel/failed 竞态只有一个 terminal event。
- cancelled 重复取消幂等，其他 terminal cancel 返回 409 和当前 snapshot。

### 16.2 Snapshot

- timezone 从 app settings 固化。
- 显式 resolution 产生 source=frontend。
- 省略 resolution 产生 12h/source=default。
- JSON null、auto 和非法 resolution 被拒绝。
- 修改 settings 对象不改变已有 snapshot。
- timestamps aware 且序列化为 UTC。

### 16.3 SSE

- session_created 在 POST 返回后一定可回放。
- 无 cursor 回放全部；合法 cursor 只回放更大 sequence。
- replay 和 wait 之间无事件丢失。
- invalid/ahead cursor 返回确定错误。
- 多订阅者收到相同有序事件。
- 某订阅断开不取消 session。
- keepalive 不编号、不回放、不写生命周期日志。
- terminal event 后 stream 关闭；terminal cursor 重连返回 204。

### 16.4 Workflow 与 cancel

- controlled fake 可完成、失败和等待取消。
- cancel 立即返回，不等待 gate 释放。
- cancel token 在 workflow 可见。
- cancel 后晚到 output 不产生 completed/failed event，也不传播下游。
- workflow 最终总能标记 quiesced。
- 两个并发 session 的 task、事件和 cancel 隔离。

### 16.5 错误与日志

- 每个 response 有 `X-Request-ID`。
- HTTP error envelope 含稳定 code、request_id 和 error_id。
- session failure SSE 的 error_id 可在结构化日志中找到。
- JSON log 每行可独立解析。
- raw query、Authorization、Cookie 和 API key 不出现在捕获日志。
- traceback 仅在后端异常日志，SSE/HTTP 不含内部路径。

### 16.6 API 与真实 server

- in-process HTTP/API 测试覆盖所有 endpoint 和错误状态。
- 真实 Uvicorn smoke 覆盖 create -> subscribe -> complete。
- 真实 Uvicorn smoke 覆盖 create -> cancel -> cancelled event。
- smoke 使用可控 fake/gate，不依赖不稳定的真实 sleep。
- 测试和 smoke 使用 Python 模块入口，支持 macOS、Windows 和 Linux。
- M1 全量测试继续通过。

真实 socket 验收入口为 `python backend/scripts/session_sse_smoke.py`。脚本通过既有 `create_app` 和 Uvicorn `Server.serve` 启动应用，将持续持有的动态 loopback socket 交给 server，避免先探测端口再重新绑定的竞争。Startup 使用事件通知，shutdown 使用 `should_exit` 并等待 server/lifespan 收尾；不发送进程信号，不调用 shell，也不添加生产测试路由。

Completed 场景使用默认 workflow，验证 pending response、created/completed 帧、终态 EOF、从 sequence 1 续传及消费完后的 204。Cancelled 场景通过 factory 注入 gate workflow，在真实 stream 收到 created/keepalive 后断开并重连，确认 disconnect 不取消；保持 gate 关闭时调用正式 cancel API，验证立即/幂等响应、唯一 cancelled 帧和 EOF，再释放 gate 并验证停止信号。短 keepalive 仅在 smoke factory 注入，不用 sleep 决定取消竞争。

现有 CI workflow 继续在 Ubuntu/macOS/Windows + Python 3.11 执行 editable install、完整 tests、health smoke，并增加 session/SSE smoke。实际三平台运行结果单独登记，不把本机成功或 YAML 配置存在视为远端 CI 通过。

## 17. M2 验收标准

1. `POST /api/analysis/sessions` 返回 HTTP 201、UUID session_id 和 pending snapshot。
2. session snapshot 正确固化 timezone、resolution 和 resolution source。
3. SSE 首次订阅能回放 session_created，并能收到唯一 terminal event。
4. `Last-Event-ID` 精确续传，无遗漏和重复。
5. 多订阅者和订阅断开不改变 session 执行。
6. cancel API 立即、幂等地取消活动 session，终态竞争规则稳定。
7. cancel 后不再向前端或下游传播结果，workflow 后续进入 quiesced。
8. fake workflow 可独立验证 completed、failed 和 cancelled。
9. HTTP/SSE error 有稳定公开结构，并能通过 ID 回溯 agent-readable JSON log。
10. M2 不包含业务 fake result、transcript、history、query understanding、真实 adapter 或前端兼容代码。
11. M2 不新增依赖并明确只支持单 worker。
12. in-process、真实 Uvicorn smoke、M1 regression 和三平台 CI 全部通过。

## 18. Deferred Contract Register

跨 Milestone 硬约束的统一事实源是 [deferred_milestone_contracts.md](../deferred_milestone_contracts.md)。后续 MR 必须遵循其中的 `MR-G01`–`MR-G14` 门禁，并在 MR 描述与承接记录中引用适用 contract ID。

本节保留 M2 产生这些延后项的上下文；中心登记中的对应范围为：

| 归属 | Contract IDs |
|---|---|
| M3 | `M3-C08`–`M3-C15` |
| M4 | `M4-C01`–`M4-C08` |
| M5 | `M5-C09`–`M5-C10` |
| M6 | `M6-C11`–`M6-C13` |
| M7 | `M7-C04`–`M7-C08` |
| M9 | `M9-C06`–`M9-C09` |
| Future | `FUTURE-C09`–`FUTURE-C13` |

### M3: Transcript Store + History APIs

- 定型 transcript item、content schema version、secret redaction 和大 payload policy。
- 持久化 session、request、lifecycle events、errors 和 final result。
- 实现持久化幂等创建：相同 `Idempotency-Key` + 相同请求返回原 session；相同 key + 不同请求返回 409。
- cancelled session 可以记录已经产生的在途结果；不增加 `produced_after_cancel` 字段，以 session status 提供上下文。
- 实现 lifecycle event 的跨重启读取或明确的历史 timeline mapping。
- 实现 `completed_with_persistence_error`：分析成功但持久化失败时发布 `session_completed`，payload 带 status 和公开 persistence warning/error_id；不得误报 `session_failed`。
- 双写持久化成功后回调标记 `persistence_confirmed`。
- 只有 `persistence_confirmed + workflow_quiesced + grace period elapsed` 同时满足时才异步清理 live state；持久化失败不得清理。
- 清理 callback 幂等，清理失败可重试且不修改 session 业务终态。
- 进程启动时把持久化中遗留的 pending/running session 归档为 failed/service_interrupted，不自动恢复。

### M4: Query Understanding + Hydrology Topology

- 定型最终 analysis request/context：日期、位置/COMID、参数、mode 等。
- 定义结束日期包含整天、session timezone 转 UTC 和自动粒度规则。
- M4 才允许产生 `time_resolution_source=auto`。
- 直接修订 M2 阶段请求模型，不建立兼容层。
- 增加 `query_understood`、`topology_ready` 的 typed payload 和 schema tests。

### M5: Evidence Tools

- 增加 evidence lifecycle payload 时继续复用 `LifecycleEvent` envelope 和 session sequence。
- 评估事件数量和 payload 体积，必要时将大内容改为 preview + reference。

### M6/M7: Reasoning 与多 Agent Workflow

- 定型 `session_completed` 中最终 inference result，不沿用 M2 fake payload。
- 定型 agent/tool/operation correlation IDs 和 stage names；继续以 session_id 为根关联 ID。
- 在真实阶段边界传播 cancellation token，保证 cancel 后不启动新 tool/agent，不向下游传播晚到结果。
- 定义 in-flight LLM/HTTP 调用的 best-effort cancellation 和 transcript 记录策略。
- 定义并发上限、排队、资源配额或 topology scheduler policy；M2 不预设。

### M9: Settings/Keys/Admin + Demo Closure

- 前端重构以最终 session + SSE API 为准；不迁移旧 WebSocket 字段或事件。
- 校对最终 OpenAPI、SSE 文档、前端消费代码和端到端 smoke。
- Admin cleanup 与 M3 live-state/persistence cleanup 协作，不删除 API keys。
- 保持 demo 单 worker 默认和启动说明。

### Future / Productionization

- 多进程/多 worker 的共享 session store、pub/sub、跨进程 cancel 和一致性策略。
- session/event retention 上限、backpressure、磁盘增长和容量告警。
- graceful drain、resume/replay 和 crash recovery。
- 认证、授权、租户隔离、限流和审计策略。
- W3C trace context、metrics 和外部 tracing/observability sink。
- 如果最终需要创建幂等性跨进程强一致，使用持久化唯一约束，而不是仅依赖内存 map。

## 19. 设计取舍

- 断开 SSE 不等于 cancel：服务端无法可靠区分用户主动关闭、网络抖动和代理超时。
- event replay 从 M2 开始：否则 session_created 总是在订阅前产生，协议存在天然竞态。
- 不公开 session_started：running 对排障有价值，但不值得冻结额外前端事件；结构化日志足够。
- 状态与 lifecycle event 原子提交：比事后补偿更简单，也避免前端永远等不到终态。
- keepalive 使用 comment：保活能力不应污染业务 sequence、日志或 transcript。
- cancel 立即返回但 workflow 后续 quiesce：用户反馈及时，同时保留在途结果审计和资源清理入口。
- lifecycle event、SSE、日志和 transcript 共享模型：复用事实，不混淆不同可见性。
- M2 全内存且单 worker：符合 demo 范围；提前引入共享存储或 pub/sub 会掩盖 M3 的真实持久化设计。
- 不生成 fake 业务结果：生命周期 fake 不应冻结未来水文结果 schema。
- 不新增依赖：M2 所需能力都能由现有框架和标准库清晰实现。
