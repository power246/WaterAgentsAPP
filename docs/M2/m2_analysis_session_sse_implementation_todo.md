# M2 Analysis Session + SSE Shell Implementation TODO

生成日期：2026-09-08

本清单用于执行 [M2 详细设计](m2_analysis_session_sse_design.md)。实现必须同时维护 [M2 API 与排障指南](m2_analysis_session_sse_api.md)，不能让代码、OpenAPI、SSE 示例和错误码漂移。

## 执行原则

- [ ] 只修改新后端 `backend/`、M2 文档和必要的上层新架构文档。
- [ ] 不读取、导入、复制、适配、枚举或测试 `backend_bk/` 与旧契约文档。
- [ ] 不修改现有前端，不添加 WebSocket 兼容层或临时调试 UI。
- [ ] 先实现最小生命周期闭环，再增加 replay、cancel、日志和真实 server smoke。
- [ ] 每一步先跑 focused tests，再跑 `backend/tests` 全量回归。
- [ ] 实现 MR 按 [跨 Milestone 延后契约](../deferred_milestone_contracts.md) 的 `MR-G01`–`MR-G14` 列出适用 contract、代码位置、测试证据和文档状态。
- [ ] 不新增生产或开发依赖。
- [ ] 不建立通用 event bus、DI framework、repository hierarchy 或 workflow framework。
- [ ] 不为 M3-M9 的未知 DTO 使用 `Any` 或任意 dict 提前定型。
- [ ] 所有真实时间通过 `ClockPort`，测试时序通过 async gate，不依赖真实 sleep。
- [ ] 每次公开状态转换原子提交 snapshot 与 lifecycle event。

## 成功标准

- [ ] POST 创建 session 并固定返回 201/pending snapshot。
- [ ] timezone、resolution 和 source 正确固化。
- [ ] SSE 可回放 created，并收到 completed/failed/cancelled 之一。
- [ ] Last-Event-ID 续传无遗漏和重复。
- [ ] SSE 断开不取消任务，多订阅者互不影响。
- [ ] cancel 立即、幂等并遵循 terminal race 规则。
- [ ] cancel 后没有前端或下游传播，workflow 最终 quiesced。
- [ ] HTTP/SSE 错误可通过 request/session/error ID 回溯结构化日志。
- [ ] 真实 Uvicorn completed/cancel smoke 通过。
- [ ] M1 tests 全绿，Linux/macOS/Windows CI 全绿。

## 已确认设计结论

- [x] 只有显式 cancel API 取消 session；SSE disconnect 不取消。
- [x] 首次订阅回放已有事件，Last-Event-ID 精确续传。
- [x] keepalive 为 15 秒 SSE comment，不编号、不回放、不记录为业务日志。
- [x] 不公开 `session_started`；running 只写结构化日志。
- [x] M2 request 只包含必填 query 和可选 6h/12h/24h resolution。
- [x] fake outcome 只通过测试注入控制，不进入生产 API。
- [x] cancel 立即返回，workflow 在后台协作式收敛。
- [x] cancelled session 的晚到结果不进入前端/下游；M3 可写 transcript，且不增加 after-cancel 字段。
- [x] 状态和 lifecycle event 在单 session 临界区原子提交。
- [x] 使用三层可观察性，但复用 CorrelationContext、LifecycleEvent 和 PublicError。
- [x] M2 单进程、单 worker；同一 worker 支持多个 session 并发。
- [x] session 和 event 保留到进程退出；M2 不做 TTL/LRU/background cleanup。
- [x] M3 按 persistence confirmed + workflow quiesced + grace period 异步清理 live state。
- [x] M2 不支持 Idempotency-Key；M3 做持久化幂等创建。
- [x] M2 不增加单 session GET，不生成 fake 业务结果，不新增依赖。
- [x] 进程 shutdown 与用户 cancel 分开，不发布伪造的 session_cancelled。
- [x] 不修改现有前端；提供独立 API 与排障文档。

## Milestone 0：基线与边界

### 基线

- [ ] 确认工作区状态，记录已有用户改动并避免覆盖。
- [ ] 使用 `backend/.venv` 或干净 Python 3.11+ 环境运行 M1 全量测试。
- [ ] 记录基线 test count、Python/FastAPI/Pydantic 版本。
- [ ] 确认 `create_app(settings=None)`、`application.state.settings`、`ClockPort`、`EventPublisherPort`、`TimeResolution` 和 `TimeResolutionSource` 的真实入口。

### 边界扫描

- [ ] 确认 M2 实现不引用现有 frontend、backend_bk 或旧契约文档。
- [ ] 确认没有提前存在的 application/session/SSE runtime 需要迁移。
- [ ] 列出 M2 将新增和修改的文件，避免无关 refactor。

### Verify

- [ ] `python -m pytest backend/tests -q`
- [ ] `git diff --check`

## Milestone 1：Application models 与 API schemas

### Application models

- [ ] 实现 `SessionStatus` 六值枚举。
- [ ] 实现 immutable `AnalysisConfigurationSnapshot`。
- [ ] 实现 immutable `AnalysisSessionSnapshot`。
- [ ] 实现四种 lifecycle event type 和版本 1 envelope。
- [ ] 实现四种 typed event payload，拒绝 type/payload 错配。
- [ ] 实现 `PublicError` 和最小 `CorrelationContext`。
- [ ] 所有 datetime 复用 M1 UTC normalization，拒绝 naive time。
- [ ] UUID、sequence 和非空 stable code 做边界校验。

### API schemas

- [ ] 实现 `CreateAnalysisSessionRequest`，query trim 后非空且禁止 extra。
- [ ] 显式提供 resolution 与省略 resolution 可被可靠区分。
- [ ] 省略 resolution 使用 12h/default；显式值使用 frontend。
- [ ] 显式 null、auto 和其他非法 resolution 返回标准 validation error。
- [ ] 实现创建/cancel success snapshot response。
- [ ] 实现统一 HTTP error envelope、typed validation violation 和可选 current session snapshot。
- [ ] success response 和 event 不回显 query。

### Tests

- [ ] model construction/validation tests。
- [ ] JSON round-trip tests。
- [ ] event discrimination tests。
- [ ] explicit/omitted/null resolution tests。
- [ ] extra field rejection tests。
- [ ] UTC/UUID/sequence validation tests。

### Verify

- [ ] `python -m pytest backend/tests/application backend/tests/api -q`

## Milestone 2：In-memory runtime 与状态机

### Runtime

- [ ] 实现 session map 和短时全局插入/查找同步。
- [ ] 实现每 session 独立 runtime、condition、event list、cancel token 和 quiesced flag。
- [ ] runtime 放在 application/analysis_sessions 内，只由 coordinator 使用。
- [ ] API 通过 coordinator 的 application-facing 方法订阅，不导入 runtime 或 infrastructure internals。
- [ ] runtime 可变对象不得进入 API 序列化。
- [ ] event list 保留到进程退出，不实现 cleanup task。
- [ ] session lookup 区分 invalid UUID 与 valid-but-not-found。

### Coordinator

- [ ] coordinator 成为唯一状态转换入口。
- [ ] 实现 pending -> running 内部转换和结构化日志调用点。
- [ ] 实现 pending/running -> completed/failed/cancelled。
- [ ] 拒绝所有 terminal overwrite。
- [ ] 同一次转换只调用一次 ClockPort.now。
- [ ] 在同一临界区中提交 public state + sequence + event 并 notify subscribers。
- [ ] 锁内不运行 workflow、I/O、logger 或不受控 callback。
- [ ] 每个 session 最多一个 terminal event。

### Race tests

- [ ] complete 与 cancel 同时到达时只有一个终态胜出。
- [ ] fail 与 cancel 同时到达时只有一个终态胜出。
- [ ] 失败方不追加第二个 terminal event。
- [ ] 两个 session 的锁、状态和 sequence 完全隔离。
- [ ] 日志 callback 失败不回滚状态或 event。

### Verify

- [ ] `python -m pytest backend/tests/application/test_session_state_machine.py -q`

## Milestone 3：Fake workflow、cancel 与 lifespan

### Workflow seam

- [ ] 定义最小 async workflow Protocol/context。
- [ ] 默认 fake 只驱动 lifecycle，不生成业务 result。
- [ ] 提供 controlled completed fake。
- [ ] 提供 controlled failed fake。
- [ ] 提供 gate-based cancellable fake。
- [ ] fake 不出现在 production request/settings/env 中。

### Task ownership

- [ ] create use case 注册 task handle 后再返回成功。
- [ ] task wrapper 在运行前提交 internal running。
- [ ] 正常返回只在仍 running 时提交 completed。
- [ ] 受控/非预期异常映射为唯一 session_failed。
- [ ] task `finally` 总是标记 workflow_quiesced。
- [ ] 消费 task exception，避免 `Task exception was never retrieved`。

### Cancel

- [ ] pending/running cancel 原子提交 cancelled event 并设置 token。
- [ ] cancelled 重复 cancel 返回相同 snapshot，不追加事件。
- [ ] 其他 terminal cancel 返回 409 和 current snapshot。
- [ ] cancel response 不等待 workflow quiesced。
- [ ] 晚到 output 记录 suppression 日志，不提交 completed/failed，不传播下游。
- [ ] 用户 cancel 不无条件 hard-cancel 整个 task。

### Lifespan

- [ ] app lifespan 跟踪所有 workflow tasks。
- [ ] shutdown 向活动 task 发出 shutdown signal 并 best-effort 收敛。
- [ ] shutdown 不发布 session_cancelled。
- [ ] shutdown 记录活动 session 原状态与 task 收敛结果。
- [ ] 测试结束没有泄漏 pending task。

### Tests

- [ ] completed/failed/cancelled controlled workflow tests。
- [ ] cancel immediate response vs delayed quiesced test。
- [ ] cancel output suppression test。
- [ ] concurrent sessions isolation test。
- [ ] application shutdown task cleanup test。

### Verify

- [ ] `python -m pytest backend/tests/application/test_session_workflow.py -q`

## Milestone 4：SSE encoder、replay 与 keepalive

### Encoder

- [ ] 实现纯函数 lifecycle event -> SSE UTF-8 frame。
- [ ] `id` 等于 sequence，`event` 等于 type，`data` 为 compact JSON envelope。
- [ ] data 中的换行和特殊字符经 JSON 正确 escaping。
- [ ] frame 以空行结束。
- [ ] 实现 `: keepalive\n\n` 常量或纯函数。

### Replay/wait

- [ ] 无 Last-Event-ID 从 sequence 1 回放。
- [ ] 合法 cursor 只返回 sequence 更大的事件。
- [ ] 在同一同步协议中完成 replay snapshot 与 wait 注册，消除丢事件窗口。
- [ ] subscriber 通过共享 immutable event list + 自身 cursor 消费，不复制长期 queue。
- [ ] publish event 后唤醒全部订阅者。
- [ ] client disconnect 只结束自己的 generator。

### Cursor/terminal

- [ ] 非整数/负数 cursor 返回 400。
- [ ] cursor ahead 返回 409。
- [ ] unknown session 返回 404。
- [ ] terminal 且有缺失事件时回放后关闭。
- [ ] terminal 且 cursor 已到最后 event 时返回 204。

### Keepalive

- [ ] 空闲 15 秒发送 comment keepalive。
- [ ] test 可注入短 interval，不等待真实 15 秒。
- [ ] keepalive 不占 sequence、不存储、不回放、不进入 lifecycle log。
- [ ] business event 和 timeout 同时发生时优先发送 event，避免无意义额外 keepalive。

### Tests

- [ ] exact frame unit tests。
- [ ] replay/cursor edge tests。
- [ ] replay-to-wait race test。
- [ ] multiple subscribers test。
- [ ] disconnect without cancel test。
- [ ] keepalive classification test。
- [ ] terminal close/204 test。

### Verify

- [ ] `python -m pytest backend/tests/api/test_analysis_events.py -q`

## Milestone 5：HTTP routes 与统一错误

### Request context

- [ ] 每个 HTTP 请求生成 server-owned UUID request_id。
- [ ] 所有 response 返回 `X-Request-ID`。
- [ ] request_id 可通过 request state/context 传给 route、handler 和 logger。
- [ ] 不信任或复用客户端伪造的 request ID。

### Routes

- [ ] 实现 POST create，成功为 201/pending。
- [ ] 实现 GET events，正确返回 event-stream/204/error。
- [ ] 实现 POST cancel，无 request body。
- [ ] 不增加单 session GET/history endpoint。
- [ ] `app.main.create_app(settings=None)` 继续可用并注册 router。
- [ ] app composition 支持 clock、ID、workflow 和 keepalive test injection。

### Errors

- [ ] 统一处理 application errors、404、409、validation 和 unexpected exception。
- [ ] 每个 non-2xx 有 error_id、request_id、stable code 和 safe message。
- [ ] 409 terminal cancel response 附 current session snapshot。
- [ ] validation response 不回显原始 input。
- [ ] exception/traceback 不进入 HTTP 或 SSE。
- [ ] stream 建立后的 workflow failure 使用 session_failed event。
- [ ] 单 subscriber write failure 不把 session 标记 failed。

### Tests

- [ ] create exact status/schema/header tests。
- [ ] cancel 200/idempotent/409/404/422 tests。
- [ ] events content-type/cache/request-id tests。
- [ ] uniform error envelope tests。
- [ ] no query echo/no traceback exposure tests。
- [ ] OpenAPI schema snapshot or focused assertions。

### Verify

- [ ] `python -m pytest backend/tests/api -q`

## Milestone 6：结构化日志与 redaction

### JSON logs

- [ ] 实现 WaterAgents application logger 的单行 JSON formatter。
- [ ] timestamp、level、event_name 和 message 稳定存在。
- [ ] request/session/error/event sequence 等 correlation 字段按上下文加入。
- [ ] 状态转换包含 from_status/to_status。
- [ ] HTTP completion 包含 method、route template、status 和 duration_ms。
- [ ] workflow exception 包含 exception type、message、traceback 和 cause chain。
- [ ] lifecycle log 直接复用 committed LifecycleEvent。
- [ ] keepalive 不逐条记录。

### Redaction

- [ ] 不记录 raw request body、query 或完整 headers。
- [ ] Authorization、Cookie、Set-Cookie、API key 和 credential names 大小写不敏感脱敏。
- [ ] PublicError 从 safe message 构造，不透传 exception string。
- [ ] redaction helper 对 nested JSON-like values 有测试。
- [ ] 日志 formatter/redactor 失败不能改变 API/session 结果。

### Correlation tests

- [ ] HTTP request_id 可在 response 和 log 同时找到。
- [ ] session_failed error_id 可在 SSE 和异常日志同时找到。
- [ ] lifecycle sequence 可在 SSE 和 lifecycle log 对齐。
- [ ] 每条 application log 可独立 JSON parse。
- [ ] 捕获日志中不存在测试 secret、raw query 或内部路径泄漏到 public response。

### Verify

- [ ] `python -m pytest backend/tests/observability -q`

## Milestone 7：真实 Server Smoke、API 文档与 CI

### Smoke

- [ ] 新增跨平台 Python smoke client/server orchestration。
- [ ] 使用真实 Uvicorn socket 验证 create -> SSE replay -> completed。
- [ ] 使用 controlled gate 验证 create -> cancel -> cancelled event。
- [ ] 验证 terminal stream close。
- [ ] smoke 不调用 shell、POSIX signal 或硬编码端口。
- [ ] smoke 使用可用 loopback port，并可靠清理 server/task。

### API docs

- [ ] 核对 `m2_analysis_session_sse_api.md` 与实际 JSON/OpenAPI。
- [ ] 核对 raw SSE frame、Last-Event-ID、204 和 keepalive。
- [ ] 核对 success/error examples 和错误码表。
- [ ] 核对 JavaScript EventSource/cancel 示例，但不修改 frontend。
- [ ] 核对排障 runbook 中所有日志 event name 和 correlation ID。
- [ ] 文档明确单 worker、内存态、重启 404 和无旧前端兼容。

### CI

- [ ] 更新或新增 M2 workflow，继续在 Ubuntu/macOS/Windows + Python 3.11 执行 editable install。
- [ ] 三平台运行完整 tests。
- [ ] 三平台运行 health smoke 和 M2 session/SSE smoke。
- [ ] CI 不依赖系统 IANA timezone database、POSIX-only command 或固定路径。

### Verify

- [ ] `python -m pytest backend/tests -q`
- [ ] `python backend/scripts/health_smoke.py`
- [ ] 运行 M2 session/SSE smoke。
- [ ] `git diff --check`

## Milestone 8：Contract Sweep 与接力

### End-to-end sweep

- [ ] 从 create request 追踪到 snapshot、runtime、workflow、event、SSE、日志和 cancel。
- [ ] 搜索并移除旧 WebSocket 名称、旧字段和临时 fake result。
- [ ] 搜索重复 event/error/session schema，保证只有一个事实模型。
- [ ] 确认生产代码没有 test-only behavior switch。
- [ ] 确认没有 session_started 公开事件。
- [ ] 确认 subscriber disconnect 不触发 cancel。
- [ ] 确认 completed/failed/cancelled 每 session 最多一个。
- [ ] 确认未实现 history、transcript、query understanding 或 persistence。

### Architecture tests

- [ ] Domain 依赖边界继续成立。
- [ ] API 只依赖 application coordinator，不直接操作 runtime 或 infrastructure 内部字段。
- [ ] application 不依赖 FastAPI request/response 类型。
- [ ] no-new-dependency assertion 更新为 M2 仍使用 M1 dependency set。
- [ ] 单 worker 限制出现在启动/API 文档，不添加多 worker 配置。

### Deferred register handoff

- [ ] 按中心登记表的 `MR-G01`–`MR-G14` 完成 MR contract evidence 和文档同步。
- [ ] M3 接力 `M3-C08`–`M3-C15`：生命周期持久化、幂等创建、cancelled transcript、persistence-error、quiesced/grace cleanup 和 startup orphan handling。
- [ ] M4 接力 `M4-C01`–`M4-C08`：final request、date mapper、auto resolution、query/topology events 和 history query 扩展。
- [ ] M5 接力 `M5-C09`–`M5-C10`：evidence event 的 payload 容量边界和延后 producer 槽位。
- [ ] M6/M7 接力 `M6-C11`–`M6-C13`、`M7-C04`–`M7-C08`：final result/history、agent/tool IDs、真实 cancellation boundaries、topology result 和 concurrency policy。
- [ ] M9 接力 `M9-C06`–`M9-C09`：前端重构、最终 API 校对、admin cleanup 和单 worker demo 说明。
- [ ] Future 接力 `FUTURE-C09`–`FUTURE-C13`：multi-worker/shared pubsub、retention/backpressure、recovery、security 和 tracing。

### Final verification

- [ ] 所有 M2 focused tests 通过。
- [ ] 所有 M1 regression tests 通过。
- [ ] completed 和 cancel 两条真实 Uvicorn smoke 通过。
- [ ] Linux/macOS/Windows CI 通过。
- [ ] `git status --short` 只包含预期文件。
- [ ] `git diff --check` 通过。

## 实现顺序摘要

```text
1. models + schemas
   -> verify: validation/round-trip/discriminated payload tests

2. in-memory runtime + state machine
   -> verify: atomic transitions/terminal race/isolation tests

3. fake workflow + cancel + lifespan
   -> verify: completed/failed/cancelled/quiesced tests

4. SSE encoder + replay + keepalive
   -> verify: exact framing/reconnect/multi-subscriber tests

5. routes + errors
   -> verify: full in-process API tests

6. JSON logs + redaction
   -> verify: correlation and leak-prevention tests

7. real server smoke + docs + CI
   -> verify: three-platform end-to-end lifecycle

8. contract sweep + handoff
   -> verify: full regression and deferred register completeness
```

## 后续非 M2

- [ ] M3：transcript/history/persistence/idempotency/completed_with_persistence_error/live-state cleanup。
- [ ] M4：final request/query understanding/date mapper/auto resolution/topology。
- [ ] M5：evidence tools 和高容量事件策略。
- [ ] M6/M7：真实 result、Agent/tool correlation 和 cancellation propagation。
- [ ] M8：transcript RAG。
- [ ] M9：settings/keys/admin、前端重构和最终 demo closure。
- [ ] Future：multi-worker、shared pub/sub、retention/backpressure、auth、rate limit、distributed tracing、resume/replay。
