# M2 Analysis Session + SSE Shell Implementation TODO

生成日期：2026-09-08

本清单用于执行 [M2 详细设计](m2_analysis_session_sse_design.md)。实现必须同时维护 [M2 API 与排障指南](m2_analysis_session_sse_api.md)，不能让代码、OpenAPI、SSE 示例和错误码漂移。

当前状态：Milestone 0–8 的实现与本机验收已完成；[最终本地记录](#milestone-8-实现与本机验收记录2026-09-08)汇总契约证据和接力入口，[启动审查修复](#milestone-8-审查修复记录2026-09-08)、[日志边界修复](#milestone-8-日志边界修复记录2026-09-09)与[跨平台 smoke 修复](#milestone-8-跨平台-smoke-修复记录2026-09-09)补充关闭、异常传播、日志级别和跨平台网络异常回归。已收到 Windows CI 的 3 项关闭探测失败；本机修复后 602 项通过，Ubuntu/macOS/Windows + Python 3.11 的修复后 CI 仍待通过，M2 整体验收与中心登记的最终承接保持待完成。各阶段记录保留当时的验收范围，以本段和最新修复记录为当前状态。

## 执行原则

- [x] 只修改新后端 `backend/`、M2 文档、必要的上层新架构文档，以及 Milestone 7 所需的 CI workflow。
- [x] 不读取、导入、复制、适配、枚举或测试 `backend_bk/` 与旧契约文档。
- [x] 不修改现有前端，不添加 WebSocket 兼容层或临时调试 UI。
- [x] 先实现最小生命周期闭环，再增加 replay、cancel、日志和真实 server smoke。
- [x] 每一步先跑 focused tests，再跑 `backend/tests` 全量回归。
- [x] 实现 MR 按 [跨 Milestone 延后契约](../deferred_milestone_contracts.md) 的 `MR-G01`–`MR-G14` 列出适用 contract、代码位置、测试证据和文档状态。
- [x] 不新增生产或开发依赖。
- [x] 不建立通用 event bus、DI framework、repository hierarchy 或 workflow framework。
- [x] 不为 M3-M9 的未知 DTO 使用 `Any` 或任意 dict 提前定型。
- [x] 所有真实时间通过 `ClockPort`，测试时序通过 async gate，不依赖真实 sleep。
- [x] 每次公开状态转换原子提交 snapshot 与 lifecycle event。

## 成功标准

- [x] POST 创建 session 并固定返回 201/pending snapshot。
- [x] timezone、resolution 和 source 正确固化。
- [x] SSE 可回放 created，并收到 completed/failed/cancelled 之一。
- [x] Last-Event-ID 续传无遗漏和重复。
- [x] SSE 断开不取消任务，多订阅者互不影响。
- [x] cancel 立即、幂等并遵循 terminal race 规则。
- [x] cancel 后没有前端或下游传播，workflow 最终 quiesced。
- [x] HTTP/SSE 错误可通过 request/session/error ID 回溯结构化日志。
- [x] 真实 Uvicorn completed/cancel smoke 通过。
- [ ] M1 tests 全绿，Linux/macOS/Windows CI 全绿（本机 M1 回归通过；远端三平台结果待取得）。

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

- [x] 确认工作区状态，记录已有用户改动并避免覆盖。
- [x] 使用 `backend/.venv` 或干净 Python 3.11+ 环境运行 M1 全量测试。
- [x] 记录基线 test count、Python/FastAPI/Pydantic 版本。
- [x] 确认 `create_app(settings=None)`、`application.state.settings`、`ClockPort`、`EventPublisherPort`、`TimeResolution` 和 `TimeResolutionSource` 的真实入口。

### 边界扫描

- [x] 确认 M2 实现不引用现有 frontend、backend_bk 或旧契约文档。
- [x] 确认没有提前存在的 application/session/SSE runtime 需要迁移。
- [x] 列出 M2 将新增和修改的文件，避免无关 refactor。

### Verify

- [x] `python -m pytest backend/tests -q`
- [x] `git diff --check`

### Milestone 0 完成记录（2026-09-08）

本阶段完成基线与边界确认；session/SSE 实现从 Milestone 1 开始。本次只更新本 TODO 和 API 文档的实现状态，没有新增生产代码、测试或依赖。

#### 工作区与本地 MR

- 当前分支：`codex/m2-analysis-session-sse`，HEAD：`0d921fd99db575ba9ff7df5c5f4fc6962745125e`（`docs: design M2 analysis session and SSE shell`）。
- 本地 M2 设计提交基于 `3f57638`，包含三份 M2 文档及 `backend_architecture_skeleton.md`、`backend_refactor_milestones.md`、`deferred_milestone_contracts.md`，共 6 个文档文件；没有 backend 实现改动。
- 开始时有 3 个未暂存改动：根目录 `.DS_Store`、`docs/M1/m1_project_shell_domain_design.md`、`docs/M1/m1_project_shell_domain_implementation_todo.md`；没有 staged 或 untracked 文件。三者作为已有用户改动保留，交付前用 SHA-256 对照确认内容未变。
- 验收期间另观察到 `.gitignore` 的未暂存改动；该文件不属于本次 M0 修改，保持原样。
- 项目目标是为 COMID 与时间窗口提供带证据链、置信度和归因的水质判断。M1 提供领域模型、UTC/当地墙上时钟窗口语义和 ports；M2 只建立创建、订阅、取消的生命周期协议。Transcript/history、query/topology、evidence、reasoning、RAG 和最终前端分别由后续里程碑承接。
- 需求依据为新架构骨架、总 milestones、M1 接力记录、M2 三份文档与中心 deferred register；旧归档和旧契约不作为 M2 的行为或测试依据。

#### 环境与测试证据

使用现有 `backend/.venv`，未重建环境或升级依赖。本机为 macOS 26.6.2 / arm64：

| 项目 | 实测版本 |
|---|---|
| Python | 3.12.14（满足 `>=3.11`） |
| FastAPI / Starlette | 0.141.1 / 1.6.0 |
| Pydantic / pydantic-settings | 2.13.5 / 2.15.0 |
| Uvicorn / tzdata | 0.52.4 / 2026.3 |
| pytest / httpx | 9.1.1 / 0.28.1 |

从仓库根执行：

```text
backend/.venv/bin/python -m pytest backend/tests -q
219 passed in 0.53s

git diff --check
```

两个命令均退出 0；diff check 无输出。上面的解释器路径仅为本次 macOS 验证记录；激活对应平台的虚拟环境后，统一使用 `python -m pytest backend/tests -q`。

M1 接力文档中的 `186 passed` 是此前完成时的历史记录，本次起点以 `219 passed` 为准。现有 `.github/workflows/backend-m1.yml` 配置了 Ubuntu/macOS/Windows + Python 3.11 的安装、全量测试与 health smoke；本次未运行远端 CI 或真实 server smoke，不将配置存在等同于三平台验收通过。M1 尚未勾选的跨平台结果继续保留，M2 在 Milestone 7 补齐自身 smoke/CI 证据。

#### 已核实的复用入口

以下路径相对 `backend/`；入口均经源码检查和实际导入确认。

| 能力 | 真实入口 | M2 接入约束 |
|---|---|---|
| App factory | `app/main.py:create_app(settings: AppSettings \| None = None) -> FastAPI`；模块级 `app` | 保持原有调用可用；在此组合 coordinator、router、lifespan 与少量测试注入点。 |
| Settings | `app/infrastructure/config/settings.py:AppSettings`，从 `app.infrastructure.config` 导出 | `application.state.settings` 当前保存传入的同一 settings 对象；session 必须复制 `analysis_timezone` 到 immutable snapshot，不能保存该可变对象引用。默认时区为 `America/Chicago`。 |
| 时钟 | `app/ports/clock.py:ClockPort.now() -> datetime`，从 `app.ports` 导出 | 同步方法，约定 aware UTC；目前没有 system clock 实现，M2 在 infrastructure 新增，每次状态转换只读取一次。 |
| Event sink | `app/ports/event_publisher.py:EventPublisherPort[EventT].publish(event) -> None`，从 `app.ports` 导出 | 异步 generic Protocol；作为未来外部 sink 的窄扩展点。权威 SSE event list 在 application runtime 内原子提交，不依赖 publisher 成功，不预造 repository/event bus。 |
| 时间粒度 | `app/domain/time/time_resolution.py:TimeResolution` 与 `DEFAULT_TIME_RESOLUTION`，从 `app.domain.time` 导出 | 仅 `6h/12h/24h`，默认 `12h`；直接复用，不新增平行枚举。 |
| 粒度来源 | `app/domain/time/time_resolution_source.py:TimeResolutionSource`，从 `app.domain.time` 导出 | 枚举含 `frontend/default/auto`；M2 只产生前两者，省略、显式值、显式 null 必须分别处理。 |
| UTC 校验 | `app/domain/common.py:UtcDatetime`、`normalize_utc_datetime` | 拒绝 naive datetime，转换为 UTC；snapshot/event 共用该规范化能力。 |

#### 边界扫描证据

边界扫描只检查新后端源码、测试、scripts、包配置和现有 CI，没有借助旧接口清单判断兼容性：

```text
rg -n -i 'backend_bk|frontend(_bk)?|backend_refactor_contract|websocket' backend/app backend/tests backend/scripts backend/pyproject.toml backend/uv.lock .github/workflows
rg -n 'SessionRuntime|SessionCoordinator|AnalysisSession|LifecycleEvent|StreamingResponse|EventSourceResponse|asyncio\.(Condition|Event|create_task)|lifespan|session_created|text/event-stream' backend/app backend/tests backend/scripts
```

- 第一项只命中 `TimeResolutionSource.FRONTEND` 的定义及其领域测试；这是新设计明确要求的稳定枚举值，不是前端代码依赖。没有归档、旧契约或 WebSocket 引用。
- 第二项无匹配。`backend/app/application/`、`api/schemas/`、`api/sse/`、`infrastructure/runtime/` 和 `infrastructure/observability/` 均不存在，没有需要迁移的 session map、task ownership、replay buffer 或 subscriber runtime。
- 实际调用 `create_app(AppSettings(...)).openapi()` 只有 `/health`；app state 只有 `settings`。M2 三个 endpoint 目前尚未注册，API 文档中的示例是待实现契约。
- 现有 Domain 与 ports 架构门禁已随全量测试通过。异步测试已有 `asyncio.run` 模式，后续用 `asyncio.Event` gate 控制时序，无需新增 pytest async 插件。

#### M2 文件改动计划

下表是后续 Milestone 1–8 的落点，不表示文件已经创建。路径相对仓库根；实现时只按当前步骤创建有实际职责的模块，必要的模块合并或拆分应同步本表。

| 操作 | 文件 | 用途 / 阶段 |
|---|---|---|
| 新增 | `backend/app/application/analysis_sessions/models.py` | immutable session/configuration、typed lifecycle payload/envelope、PublicError、CorrelationContext；Milestone 1。 |
| 新增 | `backend/app/api/schemas/analysis_sessions.py` | create request、success/error/validation response；Milestone 1。 |
| 新增 | `backend/app/application/analysis_sessions/runtime.py`、`coordinator.py`（同目录） | application-owned runtime、原子状态机与订阅入口；Milestone 2–4。 |
| 新增 | `backend/app/application/analysis_sessions/workflow.py` | 最小 workflow Protocol、取消上下文与默认生命周期 fake；Milestone 3。 |
| 新增 | `backend/app/infrastructure/runtime/system_clock.py` | `ClockPort` 的 aware UTC 实现；接入 coordinator 时创建。 |
| 新增 | `backend/app/api/sse/encoder.py` | 纯 SSE framing 与 comment keepalive；Milestone 4。 |
| 新增 | `backend/app/api/http/analysis_sessions.py`、`errors.py`（同目录） | 三个 endpoint、统一 HTTP error handler；Milestone 5。 |
| 新增 | `backend/app/api/request_context.py` | server-owned request ID、response header 与 HTTP completion 记录；Milestone 5–6。 |
| 新增 | `backend/app/infrastructure/observability/json_logging.py` | JSON formatter、correlation 与 redaction；Milestone 6。 |
| 新增 | `backend/app/application/__init__.py`、`backend/app/application/analysis_sessions/__init__.py`、`backend/app/api/schemas/__init__.py`、`backend/app/api/sse/__init__.py`、`backend/app/infrastructure/runtime/__init__.py`、`backend/app/infrastructure/observability/__init__.py` | 随各模块首次实现建立包入口，不提前建立空框架。 |
| 修改 | `backend/app/main.py` | composition、lifespan、clock/ID/workflow/keepalive 注入及 HTTP handler 注册；保留 health 与 settings 入口。 |
| 新增 | `backend/tests/application/test_session_models.py`、`test_session_state_machine.py`、`test_session_workflow.py`（同目录） | model/JSON、终态竞态、隔离、cancel/quiesced/shutdown；Milestone 1–3。 |
| 新增 | `backend/tests/api/test_analysis_schemas.py`、`test_analysis_sessions.py`、`test_analysis_events.py`（同目录） | schema、HTTP/OpenAPI/error、SSE framing/replay/keepalive/disconnect；Milestone 1、4–5。 |
| 新增 | `backend/tests/observability/test_json_logging.py`、`backend/tests/architecture/test_application_dependencies.py` | 日志关联与脱敏、API/application/runtime 依赖边界；Milestone 6、8。 |
| 新增 | `backend/scripts/analysis_session_smoke.py` | 跨平台真实 Uvicorn completed/cancel smoke，controlled fake/gate 由测试 composition 注入；Milestone 7。 |
| 修改 | `backend/tests/api/test_health.py`、`backend/tests/architecture/test_project_shell.py`、`backend/tests/architecture/test_cross_platform_ci.py` | 保留 health/import/无新增依赖断言，补充新 composition 与 M2 smoke/CI 断言。 |
| 修改 | `.github/workflows/backend-m1.yml`、`backend/README.md` | 复用现有三平台矩阵增加 M2 smoke，更新显示名称和单 worker/API 启动说明；不复制第二套 CI。 |
| 同步 | 三份 `docs/M2/m2_analysis_session_sse_*.md`、`docs/deferred_milestone_contracts.md`；语义变化时同步 `docs/backend_architecture_skeleton.md`、`docs/backend_refactor_milestones.md` | 实现证据、API/SSE 示例、MR contract evidence 与最终承接记录。 |

`backend/app/domain/`、`backend/app/ports/`、现有 settings、`backend/app/__main__.py`、health route/smoke、`backend/pyproject.toml` 和 `backend/uv.lock` 可直接复用，当前没有计划修改。CI 文件是本清单 Milestone 7 明确要求的必要上层改动，已在执行原则中列明。

#### Contract 状态与接力

Applicable contracts：`GLOBAL-C01`、`GLOBAL-C02`、`GLOBAL-C04`、`GLOBAL-C07`–`GLOBAL-C10`、`M2-C01`–`M2-C13`；执行门禁为 `MR-G01`–`MR-G14`。以下只记录 M0 已核实的起点与后续验收位置，不将设计确认登记为 `implemented`。

| Contract | 当前证据 / 代码位置 | 后续验收与文档状态 |
|---|---|---|
| `GLOBAL-C01`、`GLOBAL-C02`、`GLOBAL-C08` | 新后端边界扫描通过；本地 M2 提交仅为设计文档。 | 保持新系统独立；Milestone 8 再做实现边界 sweep。 |
| `GLOBAL-C04` | 现有 settings、UTC normalization、TimeResolution/source 入口及 M1 测试。 | Session configuration 固化由 Milestone 1–3 实现和测试，尚未承接 session 层契约。 |
| `GLOBAL-C07`、`GLOBAL-C09`、`M2-C08`、`M2-C09` | 设计已定义统一 LifecycleEvent、PublicError 和 correlation；当前无实现。 | Milestone 1、5–6 验证 typed model、公开错误、日志关联与 secret 边界。 |
| `M2-C01`–`M2-C07`、`M2-C10`–`M2-C13` | 当前不存在 session/workflow/SSE runtime。 | Milestone 2–7 用 state machine、workflow、events tests 与真实 server smoke 验证；三份 M2 文档仍为设计契约。 |
| `GLOBAL-C10` | 中心 register 已登记 M3–M9/Future 的归属与验收点。 | 本次没有新增 deferred 功能或修改契约语义；Milestone 8 完成实现后的承接记录。 |

`MR-G03/G10/G12/G13` 本阶段通过设计先行、保存用户改动和限定文档范围落实；`MR-G04/G06` 随实际数据路径实现做一致性 sweep，`MR-G07/G08/G09/G11` 的生产路径、竞态、脱敏和三平台证据由相应后续步骤完成。中心 register 的最终承接表保持未承接状态，M2 总成功标准及 Milestone 1–8 继续未勾选。

## Milestone 1：Application models 与 API schemas

### Application models

- [x] 实现 `SessionStatus` 六值枚举。
- [x] 实现 immutable `AnalysisConfigurationSnapshot`。
- [x] 实现 immutable `AnalysisSessionSnapshot`。
- [x] 实现四种 lifecycle event type 和版本 1 envelope。
- [x] 实现四种 typed event payload，拒绝 type/payload 错配。
- [x] 实现 `PublicError` 和最小 `CorrelationContext`。
- [x] 所有 datetime 复用 M1 UTC normalization，拒绝 naive time。
- [x] UUID、sequence 和非空 stable code 做边界校验。

### API schemas

- [x] 实现 `CreateAnalysisSessionRequest`，query trim 后非空且禁止 extra。
- [x] 显式提供 resolution 与省略 resolution 可被可靠区分。
- [x] 省略 resolution 使用 12h/default；显式值使用 frontend。
- [x] 显式 null、auto 和其他非法 resolution 返回标准 validation error。
- [x] 实现创建/cancel success snapshot response。
- [x] 实现统一 HTTP error envelope、typed validation violation 和可选 current session snapshot。
- [x] success response 和 event 不回显 query。

### Tests

- [x] model construction/validation tests。
- [x] JSON round-trip tests。
- [x] event discrimination tests。
- [x] explicit/omitted/null resolution tests。
- [x] extra field rejection tests。
- [x] UTC/UUID/sequence validation tests。

### Verify

- [x] `python -m pytest backend/tests/application backend/tests/api -q`

### Milestone 1 完成记录（2026-09-08）

本阶段完成 models/schema 及其校验；没有修改 app factory、health、Domain、ports、依赖或 CI。当前运行时仍只有 `/health`，没有 session runtime、workflow、SSE encoder 或 analysis route。上面的标准 validation error 是模型产生的 Pydantic `ValidationError`；HTTP 422/error handler 在 Milestone 5 接入。

#### 实现与测试位置

| 产物 | 实现 | 验证 |
|---|---|---|
| Session/configuration | `backend/app/application/analysis_sessions/models.py:SessionStatus/AnalysisConfigurationSnapshot/AnalysisSessionSnapshot` | `backend/tests/application/test_session_models.py`：UTC/UUID round-trip、嵌套 immutable、IANA timezone 与 extra 校验。 |
| Lifecycle events | 同一 `models.py` 中的四种 payload/event、`LifecycleEventType` 与 `LifecycleEvent` | 同一测试文件：四种事件 round-trip、全部 12 种 type/payload 错配、JSON Schema discriminator、strict version/sequence、created sequence 1、naive time 拒绝。 |
| PublicError/correlation | 同一 `models.py:PublicError/CorrelationContext` | 同一测试文件：UUID、正整数 sequence、snake_case code/stage、严格 bool、空 message 与 extra 拒绝。 |
| Create request/configuration mapping | `backend/app/api/schemas/analysis_sessions.py:CreateAnalysisSessionRequest.to_configuration_snapshot` | `backend/tests/api/test_analysis_schemas.py`：Python/JSON 输入的 omitted/explicit resolution、显式 null/auto 拒绝、query trim、settings 修改后的 snapshot 隔离。 |
| Success/error/validation schema | 同一 `analysis_sessions.py:AnalysisSessionResponse/HttpPublicError/AnalysisSessionErrorResponse/ValidationViolation` | 同一测试文件：success 不含 query、error 默认 null/空数组与 current snapshot、typed field path、禁止原始 input/ctx/details、SSE 只序列化 PublicError 字段。 |
| 文档示例与 schema 一致性 | 三份 M2 文档同步具体模型与字段契约 | `test_api_document_json_and_sse_data_examples_validate_against_real_models` 逐个校验 API 文档的 JSON 与四种 SSE data 示例；request/event JSON Schema 有 focused assertions。 |

只新增 `application/`、`application/analysis_sessions/`、`api/schemas/` 所需包入口，以及上述两个实现模块和两个测试文件。后续包、runtime 与框架按各自里程碑创建。

#### 已固定的边界

- 配置在 `to_configuration_snapshot` 边界固化，time resolution/source 复用 M1 枚举；省略得到 `12h/default`，显式有效值得到 `frontend`。原始 request 重序列化使用 `exclude_unset=True` 保留字段是否提供；固化后的 configuration 可直接完整 JSON round-trip。
- 事件使用四个具体 immutable envelope，`LifecycleEvent` 为按 type 区分的 union；调用方可直接构造具体 event，通用读取使用 `TypeAdapter(LifecycleEvent)`。状态机负责事件与 snapshot 的原子提交、sequence 分配及同次转换 timestamp 一致性，属于 Milestone 2。
- API response 继承公开 snapshot，HTTP error 继承 PublicError，复用字段事实；error envelope 固定 `error/session/validation`。字段校验错误定型为 `field/type/message`，省略原始 input、ctx 和任意 details map。
- 模型结构校验不等于 secret 检测或异常脱敏；安全 message 映射、Request ID header 和 redaction 仍由 Milestone 5–6 验收。未在当前模型中添加 future 业务 payload 或生产 fake outcome 开关。

#### 验证结果

使用 `backend/.venv` 的 Python 3.12.14、FastAPI 0.141.1、Pydantic 2.13.5，从仓库根执行：

```text
backend/.venv/bin/python -m pytest backend/tests/application backend/tests/api -q
122 passed in 0.62s

backend/.venv/bin/python -m pytest backend/tests -q
327 passed in 0.61s

git diff --check
```

相对 M0 的 219 项基线新增 108 项用例，全量回归通过；diff check 无输出并退出 0。没有新增依赖，也没有真实 sleep、网络、数据库或测试专用生产开关。真实 server smoke 和三平台 CI 仍由 Milestone 7 验收。

#### Contract evidence

Applicable contracts：`GLOBAL-C01`、`GLOBAL-C02`、`GLOBAL-C04`、`GLOBAL-C07`–`GLOBAL-C10`、`M2-C03`、`M2-C08`、`M2-C09`、`M2-C12`。本阶段只完成其模型/schema 部分，遵循 `MR-G01`–`MR-G14`，不将部分实现写入中心 register 的最终承接表。

| Contract | 已有实现与证据 | 尚待验收 |
|---|---|---|
| `GLOBAL-C04` | UTC snapshot 与 request/configuration mapping；对应 UTC/naive/settings isolation/JSON tests。 | Milestone 2–3 在 session 创建与状态转换中使用这些模型。 |
| `GLOBAL-C09`、`M2-C08` | 单一 typed LifecycleEvent、共享 success/error 字段；discrimination/schema/document example tests。 | Milestone 4–6 接入 SSE 和日志；M3 接入 transcript projection。 |
| `GLOBAL-C07`、`M2-C09` | PublicError、HTTP error 与 CorrelationContext；严格字段及无 input/query/traceback extra 的 schema tests。 | Milestone 5–6 的 request/error ID 生成、异常安全映射和日志脱敏。 |
| `M2-C03`、`M2-C12` | event type 只有 created/completed/failed/cancelled；completed payload 无业务 result。 | Milestone 2 的唯一终态竞争和 Milestone 3 的 lifecycle-only fake。 |
| `GLOBAL-C01`、`GLOBAL-C02`、`GLOBAL-C08`、`GLOBAL-C10` | 实现限于新后端模型，三份 M2 文档同步；未增加旧协议兼容、前端修改或新 deferred 功能。 | Milestone 8 继续完成全链路边界 sweep 和承接登记。 |

本次细化了 union、typed violation 与 optional error 字段的序列化形式，已同步详细设计和 API 文档；中心 hard contracts 的语义、归属和验收要求未改变。Milestone 2–8 与 M2 总成功标准保持未勾选。

## Milestone 2：In-memory runtime 与状态机

### Runtime

- [x] 实现 session map 和短时全局插入/查找同步。
- [x] 实现每 session 独立 runtime、condition、event list、cancel token 和 quiesced flag。
- [x] runtime 放在 application/analysis_sessions 内，只由 coordinator 使用。
- [x] API 通过 coordinator 的 application-facing 方法订阅，不导入 runtime 或 infrastructure internals。
- [x] runtime 可变对象不得进入 API 序列化。
- [x] event list 保留到进程退出，不实现 cleanup task。
- [x] session lookup 区分 invalid UUID 与 valid-but-not-found。

### Coordinator

- [x] coordinator 成为唯一状态转换入口。
- [x] 实现 pending -> running 内部转换和结构化日志调用点。
- [x] 实现 running -> completed/failed，以及 pending/running -> cancelled。
- [x] 拒绝所有 terminal overwrite。
- [x] 同一次转换只调用一次 ClockPort.now。
- [x] 在同一临界区中提交 public state + sequence + event 并 notify subscribers。
- [x] 锁内不运行 workflow、I/O、logger 或不受控 callback。
- [x] 每个 session 最多一个 terminal event。

### Race tests

- [x] complete 与 cancel 同时到达时只有一个终态胜出。
- [x] fail 与 cancel 同时到达时只有一个终态胜出。
- [x] 失败方不追加第二个 terminal event。
- [x] 两个 session 的锁、状态和 sequence 完全隔离。
- [x] 日志 callback 失败不回滚状态或 event。

### Verify

- [x] `python -m pytest backend/tests/application/test_session_state_machine.py -q`

### Milestone 2 完成记录（2026-09-08）

本阶段实现 application-owned 内存 runtime、coordinator 状态机与基础订阅入口。开工时工作区干净；仅新增本阶段的 runtime/coordinator、SystemClock、测试及包入口，更新三份 M2 文档，没有修改已有模型、schema、Domain、ports、app factory、依赖或 CI。

#### 实现入口与行为

| 入口 | 当前行为 |
|---|---|
| `backend/app/application/analysis_sessions/runtime.py:SessionRuntime` | 普通 dataclass，保存 immutable snapshot、内部 query、有序 event list、独立 condition/cancel/quiesced event、task handle 与 last_error；生产代码仅 coordinator 导入它。query 不进入 repr 或 API snapshot。 |
| `backend/app/application/analysis_sessions/coordinator.py:SessionCoordinator` | 使用短时全局 map 锁、每 session condition 和单一合法转换表；要求注入 ClockPort，可注入 ID factory 与标准库 logger。 |
| `create_session(query, configuration)` | 校验输入和生成的 UUID；用一次 clock 读取构造 pending snapshot 与 sequence 1 created event，完整 runtime 准备好后才插入 map；重复 ID 不覆盖已有 session。 |
| `mark_running/complete_session/fail_session/cancel_session` | 统一经 `_transition` 提交。running 只改 snapshot 并记录状态日志；公开终态与 event、sequence、cancel signal 同临界区提交。非法转换在读取 clock 前拒绝，重复 cancelled cancel 返回原 snapshot。 |
| `get_snapshot/subscribe` | 返回 immutable snapshot 或 typed event iterator。subscribe 在返回 iterator 前校验 UUID/lookup；从 sequence 1 读取已有事件，condition 内完成检查/等待，解锁后 yield，终态事件后关闭。 |
| `SessionNotFoundError/InvalidSessionTransitionError` | 区分合法但未知 UUID 与非法 UUID 的 Pydantic validation error；转换冲突异常携带当前 immutable session 及目标状态，竞争失败方可读取胜出终态。 |
| `_log_transition` | 解锁后调用标准库 logger，传入同一 committed LifecycleEvent、CorrelationContext、from/to status 与该次转换时间；logger/handler 失败不改变已提交结果。 |
| `backend/app/infrastructure/runtime/system_clock.py:SystemClock.now` | 同步返回 aware UTC；所有状态转换时间通过 ClockPort，coordinator 不直接读取系统时间。 |

事件列表在 coordinator 生命周期内持续保留，无 TTL/LRU、清理 task 或 subscriber queue。sequence 从列表中统一分配；running 不占 sequence。`completed_with_persistence_error` 仍只保留枚举名称，没有可进入该状态的 M2 转换。

#### 聚焦验证

测试文件：`backend/tests/application/test_session_state_machine.py`。

| 验证点 | 测试证据 |
|---|---|
| 生命周期与时间一致性 | `test_lifecycle_commits_one_timestamp_and_event_per_public_transition` 覆盖三种终态；每次成功转换只读一次 clock，snapshot/event/payload 使用相同 UTC instant，created_at/configuration 保持不变。 |
| 非法转换、幂等与终态保护 | `test_pending_cannot_complete_or_fail_before_running`、`test_running_cannot_be_started_twice`、`test_pending_cancel_is_atomic_and_idempotent`、`test_terminal_states_cannot_be_overwritten`。 |
| 终态竞争 | `test_terminal_race_has_one_winner_and_one_terminal_event` 用 gate 与真实 condition 将两个调用排队，分别让 completion/failure/cancel 先到；失败方收到胜出 snapshot，事件只有 sequence 1、2。 |
| Session 隔离与保留 | `test_sessions_have_independent_locks_events_and_cancellation` 在持有 A 的锁且 A cancel 阻塞时，让 B 创建、运行并完成；两者事件、锁、cancel/quiesced 信号独立，终态仍可再次读取和订阅。 |
| 订阅与原子可见性 | `test_commit_notifies_all_waiting_subscribers_without_exposing_runtime` 验证全部等待者被唤醒并拿到同一个已提交 event；`test_closed_subscriber_does_not_cancel_session_or_affect_other_subscribers` 验证订阅关闭不改变 session。 |
| 日志失败隔离 | `test_logging_runs_after_unlock_and_cannot_undo_committed_state` 检查两把锁均已释放、日志和 runtime 引用同一个 event，并验证真实 logging handler 抛错后 create/running/failed 仍成立。 |
| 输入与部分提交防护 | Lookup 覆盖六个 application 入口；重复/非法 UUID、非法 query、PublicError 和 naive Clock 结果均不覆盖或部分提交状态，下一次有效终态仍使用 sequence 2。 |
| 真实 clock | `test_system_clock_can_drive_a_session_without_test_configuration` 使用 SystemClock 跑通 application 创建、运行和完成。 |

竞争时序使用 `asyncio.Event` 和真实锁，不使用 sleep；测试中的 5 秒 timeout 仅用于检测死锁，不用于选择竞争胜方。白盒检查限于同步原语、内部资源身份和 logger 解锁时机，所有状态变更均调用真实 coordinator 方法，没有增加测试专用生产入口。

```text
backend/.venv/bin/python -m pytest backend/tests/application/test_session_state_machine.py -q
37 passed in 0.84s

backend/.venv/bin/python -m pytest backend/tests -q
364 passed in 2.51s

git diff --check
```

使用现有 Python 3.12.14 环境；新增 37 项用例，M1 与 Milestone 1 回归通过，diff check 无输出并退出 0。

#### 后续接入与 Contract evidence

Application 订阅入口已经建立；上面的 API 边界勾选表示提供了供路由使用的 coordinator 方法，不表示 HTTP 已接入。为验证原子提交与唤醒，本阶段实现了 Milestone 4 所需的基础事件 iterator；Milestone 4 仍须完成 cursor/Last-Event-ID、keepalive、SSE encoder 和完整 replay/race/HTTP 验收。

Milestone 3 继续实现 workflow Protocol/context、task 注册与协作式收敛，使用 runtime 的 cancel/quiesced 信号和 task handle。当前 create_session 仅创建内存态，不启动任务；M3 必须在对外创建成功前完成 task ownership。Milestone 5–6 接入 HTTP 404/409/422 映射、request/error ID、JSON formatter 与 redaction；本阶段只有结构化日志调用点。真实 server smoke 与三平台 CI 留在 Milestone 7。

Applicable contracts：`GLOBAL-C01`、`GLOBAL-C02`、`GLOBAL-C04`、`GLOBAL-C07`、`GLOBAL-C09`、`GLOBAL-C10`，以及 `M2-C01`–`M2-C06`、`M2-C08`–`M2-C12`。按 `MR-G01`–`MR-G14` 记录本阶段证据；完整 M2 smoke/CI 与端到端接入未完成，中心 register 不提前登记完整 contract 已承接。

| Contract | 当前实现与证据 | 后续验收 |
|---|---|---|
| `GLOBAL-C04`、`M2-C03`、`M2-C04` | 单一状态转换入口、UTC Clock、snapshot/event 原子提交；时间、非法转换、终态竞争和日志失败 tests。 | Workflow/HTTP 调用这些入口；完整端到端 smoke/CI。 |
| `M2-C01`、`M2-C02`、`M2-C05`、`M2-C06` | 基础订阅与显式 cancel、cancel token、幂等/终态保护；close/isolation/race tests。 | M3 在途输出抑制与 quiesced；Milestone 4–5 的 Last-Event-ID、keepalive、断线与 HTTP 语义。 |
| `GLOBAL-C07`、`GLOBAL-C09`、`M2-C08`、`M2-C09` | 复用 immutable event、PublicError 与 CorrelationContext；日志不含 raw query，log callback 与公共状态隔离。 | HTTP request/error 关联、安全异常映射和完整 JSON log/redaction。 |
| `M2-C10`、`M2-C11`、`M2-C12` | 每 session 独立资源，内存保留，无业务 result；隔离/保留/typed response tests。 | App 单 worker composition、默认 lifecycle-only fake 及真实 server smoke。 |
| `GLOBAL-C01`、`GLOBAL-C02`、`GLOBAL-C10` | 新后端范围和现有依赖集合保持不变；三份 M2 文档同步，没有新增 deferred 功能。 | Milestone 8 全链路 sweep 与最终承接登记。 |

本次将 TODO 中概括性的 pending/running 终态转换表述对齐详细设计 §6.1：只有 running 可完成/失败，pending/running 均可取消。中心 hard contracts 的语义与归属不变，Milestone 3–8 和 M2 总成功标准保持未勾选。

## Milestone 3：Fake workflow、cancel 与 lifespan

### Workflow seam

- [x] 定义最小 async workflow Protocol/context。
- [x] 默认 fake 只驱动 lifecycle，不生成业务 result。
- [x] 提供 controlled completed fake。
- [x] 提供 controlled failed fake。
- [x] 提供 gate-based cancellable fake。
- [x] fake 不出现在 production request/settings/env 中。

### Task ownership

- [x] create use case 注册 task handle 后再返回成功。
- [x] task wrapper 在运行前提交 internal running。
- [x] 正常返回只在仍 running 时提交 completed。
- [x] 受控/非预期异常映射为唯一 session_failed。
- [x] task `finally` 总是标记 workflow_quiesced。
- [x] 消费 task exception，避免 `Task exception was never retrieved`。

### Cancel

- [x] pending/running cancel 原子提交 cancelled event 并设置 token。
- [x] cancelled 重复 cancel 返回相同 snapshot，不追加事件。
- [x] 其他 terminal cancel 产生携带 current snapshot 的冲突异常，供 Milestone 5 映射 HTTP 409。
- [x] cancel response 不等待 workflow quiesced。
- [x] 晚到 output 记录 suppression 日志，不提交 completed/failed，不传播下游。
- [x] 用户 cancel 不无条件 hard-cancel 整个 task。

### Lifespan

- [x] app lifespan 跟踪所有 workflow tasks。
- [x] shutdown 向活动 task 发出 shutdown signal 并 best-effort 收敛。
- [x] shutdown 不发布 session_cancelled。
- [x] shutdown 记录活动 session 原状态与 task 收敛结果。
- [x] 测试结束没有泄漏 pending task。

### Tests

- [x] completed/failed/cancelled controlled workflow tests。
- [x] cancel immediate response vs delayed quiesced test。
- [x] cancel output suppression test。
- [x] concurrent sessions isolation test。
- [x] application shutdown task cleanup test。

### Verify

- [x] `python -m pytest backend/tests/application/test_session_workflow.py -q`

### Milestone 3 完成记录（2026-09-08）

本阶段完成 async workflow、后台 task ownership、协作式取消和 app lifespan 收敛。开始时工作区干净，基于本地 Milestone 2 提交 `9767c0f`；只修改新后端实现、application tests 和三份 M2 文档，无新增依赖、生产 fake outcome 开关或业务 result。

#### 实现与验收

- `backend/app/application/analysis_sessions/workflow.py`：最小 `AnalysisWorkflow` Protocol、frozen `WorkflowContext`、只读 `CancellationToken` 和默认 lifecycle-only fake。context 复用 running snapshot 与 configuration，query 不进入 repr；controlled/gated fake 仅在测试文件中定义。
- `backend/app/application/analysis_sessions/coordinator.py`：创建时在 map 临界区登记 task/session，通过 registration gate 保证 eager task factory 也不会提前执行 workflow；task 创建失败不留下未被任务持有的 session。返回值始终是创建时的 pending snapshot。
- wrapper 提交 internal running，正常返回只能从 running 提交 completed；`WorkflowFailed` 与非预期异常通过同一入口提交唯一 failed event。公开错误使用固定安全摘要，日志复用 error ID 并记录 exception type；不记录原始异常文本、query 或 headers。finally 标记 quiesced，done callback 消费异常并处理 task 首次执行前被取消的情况。
- `cancel_session` 原子提交 cancelled/event 和取消信号，幂等且不等待 quiesced、不调用整个 task 的 `Task.cancel()`。通过 gate 验证晚到成功、失败和忽略 token 的返回均不会覆盖终态；阶段边界 checkpoint 阻止下游传播。
- `backend/app/main.py:create_app` 在 lifespan 内创建并保存 coordinator，关闭时对活动 task 发出 stop signal，默认给予 1 秒协作式宽限期，再取消未结束的 task 并 await 清理。关闭后不能创建 session，重复 shutdown 幂等；只记录 `service_shutdown` 的原状态及 `quiesced/task_cancelled` 结果，不伪造 cancelled event。
- `backend/tests/application/test_session_state_machine.py` 的 37 项回归通过真实创建路径注入等待型 workflow，统一在退出时 shutdown；继续验证原子时间/事件、通知、隔离、终态竞争和日志失败隔离。
- `backend/tests/application/test_session_workflow.py` 新增 29 项测试，覆盖默认/eager task 注册、受控成功/失败、提前取消与延迟 quiesced、晚到输出、真实 workflow/cancel 竞争、多 session 隔离、创建/shutdown 竞争、协作式/兜底任务清理、重入 lifespan、task factory 与 wrapper 失败。时序使用 async gate，5 秒 timeout 仅作死锁保护；测试退出检查 task 完成和 quiesced。

#### Verify

从仓库根使用现有 Python 3.12.14 环境执行：

```text
backend/.venv/bin/python -m pytest backend/tests/application/test_session_workflow.py -q
29 passed

backend/.venv/bin/python -m pytest backend/tests/application/test_session_workflow.py backend/tests/application/test_session_state_machine.py -q
66 passed in 0.54s

backend/.venv/bin/python -m pytest backend/tests -q
393 passed in 1.05s

backend/.venv/bin/python backend/scripts/health_smoke.py
{"status":"healthy"}

git diff --check
```

命令均退出 0，diff check 无输出。Health smoke 使用现有脚本，沙箱禁止 loopback bind 后经执行权限审批成功运行临时本机 Uvicorn；它验证 app/lifespan 的启动、health 和退出，不替代 Milestone 7 的真实 session completed/cancel smoke。新增应用代码只依赖跨平台 asyncio/标准库；Python 3.11 不提供 eager task factory，该专项测试按能力跳过。未运行远端 Linux/macOS/Windows CI，不将本机通过视为三平台完成。

#### Contract evidence 与后续接入

Applicable contracts：`GLOBAL-C01`、`GLOBAL-C02`、`GLOBAL-C04`、`GLOBAL-C07`、`GLOBAL-C09`、`GLOBAL-C10`，以及 `M2-C01`–`M2-C06`、`M2-C08`–`M2-C13`。按 `MR-G01`–`MR-G14` 记录阶段证据；未改变中心 register 的硬约束或归属，也不提前登记完整 M2 contract 已承接。

| Contract | 当前实现与测试证据 | 文档与剩余验收 |
|---|---|---|
| `GLOBAL-C04`、`M2-C03`、`M2-C04` | wrapper 调用单一状态转换入口；state machine 原子时间/event 回归及 `test_real_workflow_and_cancel_race_commit_one_terminal`。 | 详细设计 §5–6；HTTP/SSE 集成和完整 smoke/CI 由 Milestone 4–7 验收。 |
| `M2-C01`、`M2-C02`、`M2-C05`、`M2-C06` | `cancel_session`、token/checkpoint、输出抑制；`test_pending_cancel_does_not_enter_workflow`、`test_cancel_returns_before_quiescence_and_suppresses_late_output`，以及原有独立订阅/关闭回归。 | 详细设计 §9–10、API cancel 说明；Last-Event-ID、keepalive、SSE disconnect 和 HTTP 409 映射仍由 Milestone 4–5 接入。 |
| `GLOBAL-C07`、`GLOBAL-C09`、`M2-C08`、`M2-C09` | 复用 snapshot/event/PublicError，失败公开摘要和 error ID；`test_controlled_outcomes_use_running_context_and_safe_unique_terminal` 注入含 secret 的 query/异常，验证公开 event 和结构化记录不含原文。 | 详细设计 §10–12、API 状态说明；request ID、JSON formatter、完整日志 redaction 和异常诊断由 Milestone 5–6 验收。 |
| `M2-C10`、`M2-C11`、`M2-C12` | 每 lifespan 独立 runtime，task/session 保留，默认 fake 无业务结果；默认完成、多 session 隔离及重入 lifespan tests。 | 详细设计 §10、§13–14；真实 session smoke 与三平台 CI 在 Milestone 7 验收。 |
| `M2-C13` | lifespan/shutdown 信号与显式 cancel 分离；协作式停止、硬取消后清理、取消未收敛 task、多 task 和创建/关闭竞争 tests。 | 详细设计 §13.4、API cancel 说明；M3 持久化遗留 `service_interrupted` 处理归属不变。 |
| `GLOBAL-C01`、`GLOBAL-C02`、`GLOBAL-C10` | 新后端范围、依赖集合和旧系统隔离保持不变；fake 仅测试注入、完整 backend 回归及 architecture gates 通过。 | 三份 M2 文档同步；Milestone 8 全链路 sweep 与最终承接登记待完成。 |

本阶段的 409 勾选明确指 application 冲突类型和 current snapshot；HTTP 路由/状态码尚未实现，OpenAPI 仍只有 `/health`。M2 总成功标准与 Milestone 4–8 保持未勾选；本阶段没有新增 deferred contract。

## Milestone 4：SSE encoder、replay 与 keepalive

### Encoder

- [x] 实现纯函数 lifecycle event -> SSE UTF-8 frame。
- [x] `id` 等于 sequence，`event` 等于 type，`data` 为 compact JSON envelope。
- [x] data 中的换行和特殊字符经 JSON 正确 escaping。
- [x] frame 以空行结束。
- [x] 实现 `: keepalive\n\n` 常量或纯函数。

### Replay/wait

- [x] 无 Last-Event-ID 从 sequence 1 回放。
- [x] 合法 cursor 只返回 sequence 更大的事件。
- [x] 在同一同步协议中完成 replay snapshot 与 wait 注册，消除丢事件窗口。
- [x] subscriber 通过共享 immutable event list + 自身 cursor 消费，不复制长期 queue。
- [x] publish event 后唤醒全部订阅者。
- [x] client disconnect 只结束自己的 generator。

### Cursor/terminal

- [x] 非整数/负数 cursor 产生 `InvalidEventCursorError`，供 Milestone 5 映射 HTTP 400。
- [x] cursor ahead 产生 `EventCursorAheadError`，供 Milestone 5 映射 HTTP 409。
- [x] unknown session 产生 `SessionNotFoundError`，供 Milestone 5 映射 HTTP 404。
- [x] terminal 且有缺失事件时回放后关闭。
- [x] terminal 且 cursor 已到最后 event 时返回 204。

### Keepalive

- [x] 空闲 15 秒发送 comment keepalive。
- [x] test 可注入短 interval，不等待真实 15 秒。
- [x] keepalive 不占 sequence、不存储、不回放、不进入 lifecycle log。
- [x] business event 和 timeout 同时发生时优先发送 event，避免无意义额外 keepalive。

### Tests

- [x] exact frame unit tests。
- [x] replay/cursor edge tests。
- [x] replay-to-wait race test。
- [x] multiple subscribers test。
- [x] disconnect without cancel test。
- [x] keepalive classification test。
- [x] terminal close/204 test。

### Verify

- [x] `python -m pytest backend/tests/api/test_analysis_events.py -q`

### Milestone 4 完成记录（2026-09-08）

本阶段完成 SSE encoder、cursor/replay、keepalive 以及 stream/204 response 构造与订阅清理。开始时工作区干净，基于本地 Milestone 3 提交 `092f28e`，全量基线为 393 项通过。改动限定于新后端、API tests 和三份 M2 文档，没有新增依赖、业务 DTO 或生产请求开关。

#### 实现与验收

- `backend/app/api/sse/encoder.py`：纯函数 `encode_event(LifecycleEvent) -> bytes` 直接序列化共享 immutable event；UTF-8、单行 compact JSON、严格对应的 id/type，以及结束空行。`KEEPALIVE_FRAME` 固定为无 ID/data 的 comment。API 文档四个 raw frame 与 encoder 逐字节一致，额外覆盖中文、emoji、引号、反斜杠、CR/LF、tab 与 NUL escaping。
- `backend/app/application/analysis_sessions/coordinator.py:subscribe`：开始 stream 前校验 cursor、session 是否存在和终态。缺失 cursor 从 0 开始；数字字符串允许前导零，只接受 ASCII 十进制；严格 int 不接受 bool/float。超长合法数字先与当前最大 sequence 比较，不依赖 Python 的超长字符串转 int。非法、越界和 unknown session 分别产生安全的 application 异常；已消费完终态返回 `None`。
- `_events`：每 subscriber 持有独立 cursor，共享原 immutable event 对象，不新增长期队列。读取、等待与超时后复查都使用同一个 session condition；公开终态发送完立即结束。真实 `asyncio.Timeout.reschedule` 配合 gate 验证超时已触发、订阅者等待重新取得锁期间发布终态时，仍优先交付 event。
- `backend/app/main.py:create_app` 和 coordinator 提供 `keepalive_interval` keyword-only 注入，默认 15 秒并校验有限正值。心跳不写事件列表、日志或 session 时间，不更新 sequence；计时使用当前订阅 task 的 `asyncio.timeout`，无额外心跳 task。
- `backend/app/api/sse/response.py:create_event_response`：合法订阅构造 `200 text/event-stream; charset=utf-8`、`Cache-Control: no-cache`；已消费完终态构造无 body 的 204。response 拥有 frame/subscription generator，在结束、断线、task cancellation 和发送失败时关闭两者；不修改 session，也不取消 workflow。
- `backend/tests/api/test_analysis_events.py` 新增 58 项测试：exact frame、Markdown frame 一致性、成功/失败/取消终态的 replay/204、非法与超长 cursor、UUID/unknown session、replay-to-wait 三个时序窗口、真实 timer 竞争、独立订阅、keepalive 分类、ASGI disconnect/send failure 清理。ASGI 测试直接调用真实 response factory 和 response，不注册临时测试路由或 fake outcome 参数。

#### Verify

使用现有 Python 3.12.14 环境，从仓库根执行：

```text
backend/.venv/bin/python -m pytest backend/tests/api/test_analysis_events.py -q
58 passed in 0.58s

backend/.venv/bin/python -m pytest backend/tests/application/test_session_state_machine.py backend/tests/application/test_session_workflow.py -q
66 passed in 0.54s

backend/.venv/bin/python -m pytest backend/tests -q
451 passed in 0.92s

backend/.venv/bin/python backend/scripts/health_smoke.py
{"status":"healthy"}

git diff --check
```

命令均退出 0；diff check 无输出，新文件另检查行尾空白。时序测试用 async gate 或主动触发真实 timeout；短 keepalive interval 不用于断言墙钟耗时。ASGI 层验证了 200/204、逐帧交付与断线清理，现有本机 Uvicorn health smoke 通过；正式 session/SSE 路由与真实 SSE server smoke 分别属于 Milestone 5 和 7。未运行远端 CI，不能将本机通过写成 Linux/macOS/Windows 三平台验收完成。

#### Contract evidence 与后续接入

Applicable contracts：`GLOBAL-C01`、`GLOBAL-C02`、`GLOBAL-C04`、`GLOBAL-C07`、`GLOBAL-C09`、`GLOBAL-C10`，以及 `M2-C01`–`M2-C13`。按 `MR-G01`–`MR-G14` 记录阶段证据；中心 register 的硬约束和归属不变，完整 M2 contract 承接仍等待后续 HTTP、日志、smoke/CI 和最终 sweep。

| Contract | 当前实现与测试证据 | 文档与剩余验收 |
|---|---|---|
| `GLOBAL-C04`、`GLOBAL-C09`、`M2-C02`、`M2-C03`、`M2-C04`、`M2-C08` | `subscribe/_events/encode_event` 复用同一 event、sequence 和 UTC timestamp；exact frame、terminal replay/204、replay-to-wait race 与 state machine 回归。 | 详细设计 §4、§8，API raw frames/续传说明；Milestone 5 路由及 Milestone 7 真实 server 验收。 |
| `M2-C01`、`M2-C05`、`M2-C06`、`M2-C10`、`M2-C13` | response 只关闭本订阅；`test_disconnect_or_write_failure_closes_only_its_subscription`、multiple subscribers，以及原有 workflow/cancel/shutdown 回归。 | 详细设计 §8.5、API 实现状态；正式 HTTP disconnect/cancel 和真实 server smoke 由 Milestone 5、7 验收。 |
| `M2-C07`、`M2-C11` | 默认 15 秒、独立 None transport 信号、无额外 task，事件保留；keepalive 的事件/时间/log/task 检查，实际 timeout 与 publish 竞争测试。 | 详细设计 §8.4、API keepalive；Milestone 6 日志 formatter 与 Milestone 7 server/CI 验收。 |
| `GLOBAL-C07`、`M2-C09`、`M2-C12` | encoder 不增加 query/业务 result，cursor 异常不保存原始输入；终态 SSE safe error、UUID/error ID、JSON escaping 与 M3 secret tests 回归。 | API 错误和 raw frame 说明；request correlation、统一 400/404/409/422 envelope 与完整 redaction 在 Milestone 5–6 接入。 |
| `GLOBAL-C01`、`GLOBAL-C02`、`GLOBAL-C10` | 只扩展现有 application 入口和 API SSE adapter，不引入兼容分支、依赖或后台清理；architecture gates 和全量回归通过。 | 三份 M2 文档同步；Milestone 8 最终全链路 sweep/承接登记待完成。 |

本阶段 400/404/409 勾选指可在发送前确定的 application 错误分类，最终 HTTP 状态码和统一 envelope 仍由 Milestone 5 接入；200/204 已在真实 response 的 ASGI 调用中验证。当前运行时 OpenAPI 仍只有 `/health`，没有提前注册 session 路由。M2 总成功标准与 Milestone 5–8 保持未勾选，无新增 deferred contract。

## Milestone 5：HTTP routes 与统一错误

### Request context

- [x] 每个 HTTP 请求生成 server-owned UUID request_id。
- [x] 所有 response 返回 `X-Request-ID`。
- [x] request_id 可通过 request state/context 传给 route、handler 和 logger。
- [x] 不信任或复用客户端伪造的 request ID。

### Routes

- [x] 实现 POST create，成功为 201/pending。
- [x] 实现 GET events，正确返回 event-stream/204/error。
- [x] 实现 POST cancel，无 request body。
- [x] 不增加单 session GET/history endpoint。
- [x] `app.main.create_app(settings=None)` 继续可用并注册 router。
- [x] app composition 支持 clock、ID、workflow 和 keepalive test injection。

### Errors

- [x] 统一处理 application errors、404、409、validation 和 unexpected exception。
- [x] 每个 non-2xx 有 error_id、request_id、stable code 和 safe message。
- [x] 409 terminal cancel response 附 current session snapshot。
- [x] validation response 不回显原始 input。
- [x] exception/traceback 不进入 HTTP 或 SSE。
- [x] stream 建立后的 workflow failure 使用 session_failed event。
- [x] 单 subscriber write failure 不把 session 标记 failed。

### Tests

- [x] create exact status/schema/header tests。
- [x] cancel 200/idempotent/409/404/422 tests。
- [x] events content-type/cache/request-id tests。
- [x] uniform error envelope tests。
- [x] no query echo/no traceback exposure tests。
- [x] OpenAPI schema snapshot or focused assertions。

### Verify

- [x] `python -m pytest backend/tests/api -q`

### Milestone 5 完成记录（2026-09-08）

本阶段接入正式 HTTP routes、server-owned request ID、统一错误 envelope 和 request/session/error 关联。开始时工作区干净，基于本地 Milestone 4 提交 `99233a7`，全量基线为 451 项通过。改动仅涉及新后端、相关 tests 和三份 M2 文档，无新增依赖、业务结果 DTO、前端或旧系统改动。

#### 实现与验收

- `backend/app/api/http/analysis_sessions.py` 注册 POST create、GET events 和 POST cancel；复用现有 request/response schema、coordinator 与 SSE response factory。create 固定返回 201/pending，按 request/settings 固化 timezone/resolution/source；cancel 无 body，成功立即返回 200，终态冲突返回 409/current snapshot。没有单 session detail/history GET。
- `backend/app/api/http/request_context.py` 使用纯 ASGI middleware，不缓冲 streaming；每个 HTTP 请求生成新 UUID，写入 request state 并给 response 添加 `X-Request-ID`。忽略客户端伪造 header；header/validation/unknown route/500/204/health/OpenAPI 均覆盖。请求结束恢复 ContextVar，后台 workflow 继承创建请求 ID，独立订阅/cancel 使用各自 ID。
- `backend/app/application/correlation.py` 提供 request ID ContextVar；coordinator 原有 lifecycle/activity log 继续使用同一 `CorrelationContext`、snapshot 和 event，并读取当前 request ID。没有新增第二套 request/session/error 事实模型。
- `backend/app/api/http/errors.py` 将 application、HTTP、validation 和 headers 前的未处理异常映射为 `AnalysisSessionErrorResponse`。HTTP error ID、request ID 与日志一致；已知字段名保留，未知 key 映射为 `unknown_field`，不认识的校验 type 映射为 `invalid_value`。公开摘要使用固定安全消息，不复制 input、ctx、原始 msg、exception detail 或 traceback。
- 支持 `session_not_found`、`invalid_event_cursor`、`event_cursor_ahead`、`session_already_terminal`、`invalid_request` 和 `internal_error`。额外明确未知路由 `not_found`、方法不支持 `method_not_allowed`、关闭中的 coordinator `service_unavailable`，以及框架 HTTP 错误兜底 `http_error`；保留 405 的 Allow，禁用自动尾斜杠重定向。
- headers 发出后不再构造 HTTP 错误 JSON；workflow failure 通过 `session_failed` 发送。真实路由的 ASGI 测试验证 keepalive 期间仍可并发 cancel，disconnect/send failure 只结束本订阅，encoder 异常不会追加第二套 HTTP response 或改写 session 终态。
- OpenAPI 明确 201/200/204、SSE media type、UUID path、Last-Event-ID、无 body 的 cancel、统一错误 schema 和 `X-Request-ID`。原有 workflow 测试中“OpenAPI 只有 health”的阶段性断言由 `test_openapi_describes_only_the_supported_routes_and_actual_envelopes` 的完整路由断言承接。

#### Verify

使用现有 Python 3.12.14 环境，从仓库根执行：

```text
backend/.venv/bin/python -m pytest backend/tests/api -q
168 passed in 0.91s

backend/.venv/bin/python -m pytest backend/tests -q
502 passed in 1.62s

backend/.venv/bin/python backend/scripts/health_smoke.py
{"status":"healthy"}

git diff --check
```

命令均退出 0；diff check 无输出，新文件另检查行尾空白。新增 `backend/tests/api/test_analysis_sessions.py` 共 51 项测试，涵盖配置组合、创建/取消/续传、pending 与 running cancel、HTTP error/UUID/cursor、secret 输入、请求 ID 并发隔离、后台继承、日志失败、OpenAPI 和完整应用的 streaming 边界。fake 仅替换 workflow/clock/ID 或受控异常，测试使用真实 production routes，不新增测试路由/特殊请求参数。

本机真实 Uvicorn health smoke 通过；正式 session completed/cancel server smoke 及 Linux/macOS/Windows CI 仍由 Milestone 7 验收。本阶段提供结构化日志调用点和关联 ID，不将其等同于 Milestone 6 的 JSON formatter、duration_ms、通用 redaction 与完整异常诊断。

#### Contract evidence 与后续接入

Applicable contracts：`GLOBAL-C01`、`GLOBAL-C02`、`GLOBAL-C04`、`GLOBAL-C07`、`GLOBAL-C09`、`GLOBAL-C10`，以及 `M2-C01`–`M2-C13`。按 `MR-G01`–`MR-G14` 记录本阶段证据，中心 register 硬约束和归属保持不变；完整 M2 承接仍等待 Milestone 6–8 的日志、smoke/CI 和最终 sweep。

| Contract | 当前实现与测试证据 | 文档与剩余验收 |
|---|---|---|
| `GLOBAL-C04`、`GLOBAL-C09`、`M2-C03`、`M2-C04`、`M2-C08` | HTTP routes 复用配置/snapshot/event；`test_create_exact_pending_snapshot_and_configuration` 覆盖省略/显式 resolution 与两个 IANA timezone，terminal conflict 复用当前 snapshot；状态机回归保持原子性。 | 详细设计 §5、§9、§11，API schemas/示例/OpenAPI；Milestone 7 真实 session server smoke。 |
| `M2-C01`、`M2-C02`、`M2-C05`、`M2-C06`、`M2-C07` | 真实 HTTP cancel/replay/204/cursor error；`test_cancel_is_immediate_idempotent_and_replays_one_terminal`、stream keepalive 并发取消、disconnect/send failure 测试，及原有晚到输出/竞争回归。 | API 订阅/取消说明；Milestone 6 日志层验收、Milestone 7 server/CI。 |
| `GLOBAL-C07`、`M2-C09` | 固定安全错误、未知 field key 保护、server-owned request ID 与 ContextVar；validation/500/HTTPException detail/no-query/no-traceback、并发请求隔离、workflow failure error ID 关联和 logger failure tests。 | 详细设计 §11–12、API ID/error 表；Milestone 6 通用 JSON/redaction、duration_ms 和经脱敏的完整异常诊断。 |
| `M2-C10`、`M2-C11`、`M2-C12`、`M2-C13` | 现有单 worker/app lifespan/task ownership 不变，不复用 Idempotency-Key、不生成业务 result；默认完成、重复创建、503 shutdown 和 workflow/lifespan 全量回归。 | API 限制和错误码说明；Milestone 7 跨平台及 server 验收，M3 持久化/遗留处理归属不变。 |
| `GLOBAL-C01`、`GLOBAL-C02`、`GLOBAL-C10` | 新后端范围、现有依赖和真实入口保持；完整 OpenAPI routes 断言承接阶段性 health-only 断言，architecture gates 与全量回归通过。 | 三份 M2 文档同步；Milestone 8 最终 contract sweep/承接登记。 |

Milestone 3–4 完成记录中待 HTTP 接入的 200/204/400/404/409/422 项已由本阶段真实路由测试承接。M2 总成功标准与 Milestone 6–8 保持未勾选；未新增 deferred contract，也未把本机测试当作完整跨平台验收。

## Milestone 6：结构化日志与 redaction

### JSON logs

- [x] 实现 WaterAgents application logger 的单行 JSON formatter。
- [x] timestamp、level、event_name 和 message 稳定存在。
- [x] request/session/error/event sequence 等 correlation 字段按上下文加入。
- [x] 状态转换包含 from_status/to_status。
- [x] HTTP completion 包含 method、route template、status 和 duration_ms。
- [x] workflow exception 包含 exception type、message、traceback 和 cause chain。
- [x] lifecycle log 直接复用 committed LifecycleEvent。
- [x] keepalive 不逐条记录。

### Redaction

- [x] 不记录 raw request body、query 或完整 headers。
- [x] Authorization、Cookie、Set-Cookie、API key 和 credential names 大小写不敏感脱敏。
- [x] PublicError 从 safe message 构造，不透传 exception string。
- [x] redaction helper 对 nested JSON-like values 有测试。
- [x] 日志 formatter/redactor 失败不能改变 API/session 结果。

### Correlation tests

- [x] HTTP request_id 可在 response 和 log 同时找到。
- [x] session_failed error_id 可在 SSE 和异常日志同时找到。
- [x] lifecycle sequence 可在 SSE 和 lifecycle log 对齐。
- [x] 每条 application log 可独立 JSON parse。
- [x] 捕获日志中不存在测试 secret、raw query 或内部路径泄漏到 public response。

### Verify

- [x] `python -m pytest backend/tests/observability -q`

### Milestone 6 完成记录（2026-09-08）

本阶段完成 WaterAgents 单行 JSON 日志、请求耗时、异常诊断和输出边界 redaction。开始时工作区干净，基于本地 Milestone 5 提交 `e76afb4`；全量基线为 502 项通过。改动仅涉及新后端、相关测试、backend README 与三份 M2 文档，没有新增依赖或修改业务/HTTP/SSE schema。

#### 实现与验收

- `backend/app/infrastructure/observability/json_logging.py`：formatter 固定核心字段，展平 `CorrelationContext`，直接序列化 committed `LifecycleEvent` 和既有 `PublicError`。显式选择可输出字段，不导出任意 LogRecord extra；每行独立 JSON parse，换行和非 ASCII 字符使用 JSON 转义，非 UTF-8 控制台也能无损解析。
- `backend/app/main.py` 在 lifespan 中按 `AppSettings.log_level` 安装并复用 `wateragents` 自有 handler。单行 JSON 写 stderr，不向 root 传播，不修改 Uvicorn/第三方 handler，也不在 app import/factory 阶段安装日志。handler/级别为进程级，不构建 app 级日志管理框架。
- `backend/app/infrastructure/observability/redaction.py`：递归复制 JSON-like 值，按大小写不敏感凭据 key 以及 query/body/headers policy 替换敏感内容；诊断文本处理具名 credential、Authorization/Cookie、多 Cookie 值、Bearer/Basic 和 URL 用户凭据。屏蔽已知 query 的原文、repr 和 JSON 转义形式；不读取、缓存或输出完整请求 body/headers。
- `coordinator.py` 在已有错误和晚到异常调用点传递 exception；formatter 保留类型、安全化消息和标准 cause/context/group traceback 的位置，省略源码行和局部变量。公开错误继续由固定 safe message 构造；异常日志和 session_failed SSE 使用相同 error ID。Cancel 获胜后的异常只记 suppression，不产生 failed event 或新的公开 error ID。
- `api/http/request_context.py` 与 `errors.py` 用既有 `ClockPort` 记录请求开始/结束和 duration_ms，时钟回拨时为 0，SSE duration 覆盖整个订阅。HTTP 日志只有 route template，没有完整 URL。未处理异常提供脱敏诊断；validation/HTTPException 不格式化可能带 input/detail 的原始异常。Headers 后异常记 `http_stream_failed`，不追加错误 envelope 或改变 session。
- Formatter/redactor/序列化失败只生成固定安全 `log_format_failed` JSON，timestamp 为 null；写入失败丢弃记录，避免默认 logging error handler 回显原始异常。所有日志调用继续在业务提交之后、session condition 之外，失败不改变 API、terminal event 或 quiesced。
- `backend/tests/conftest.py:application_logs` 捕获真实 formatter 的 JSON 输出，并在测试结束恢复 logger 配置。原 workflow/API 安全断言移到最终 JSON 输出边界，同时保留对原始 committed event 对象、CorrelationContext 和状态的断言；不会以删除覆盖掩盖原始 exception 进入诊断链后的变化。Keepalive 原有无日志断言增加 JSON 行数/内容检查。

#### Verify

使用现有 Python 3.12.14 环境，从仓库根执行：

```text
backend/.venv/bin/python -m pytest backend/tests/observability -q
64 passed in 1.96s

backend/.venv/bin/python -m pytest backend/tests -q
566 passed in 1.80s

backend/.venv/bin/python backend/scripts/health_smoke.py
{"status":"healthy"}

git diff --check
```

上述验收均退出 0。Health smoke 首次受 sandbox loopback bind 限制，允许本机临时端口后重跑通过并清理子进程。新增 observability 测试共 64 项，涵盖 nested redaction、转义 query、单行字段、cause/context/group、真实 stderr 输出、模拟非 UTF-8 控制台编码、并发 ID 关联、SSE/event 对齐、HTTP 耗时/回拨、validation/未知路由保护、取消后晚到异常，以及 formatter/redactor/writer 故障下 completed/failed/cancelled 九种组合。复用已有真实 routes 和注入 workflow，不增加测试专用 production 路由或行为开关。

本次未运行远端 CI；正式 completed/cancel 真实 SSE server smoke 与 Linux/macOS/Windows CI 仍由 Milestone 7 完成。本机 health smoke 不等同于该验收。

#### Contract evidence 与后续接入

Applicable contracts：`GLOBAL-C01`、`GLOBAL-C02`、`GLOBAL-C04`、`GLOBAL-C07`、`GLOBAL-C09`、`GLOBAL-C10`，以及 `M2-C01`–`M2-C13`。按 `MR-G01`–`MR-G14` 记录阶段证据；中心 register 的硬约束与归属不变。

| Contract | 当前实现与测试证据 | 文档与剩余验收 |
|---|---|---|
| `GLOBAL-C07`、`M2-C09` | `JsonFormatter/redact/log_http/_log_activity`；nested/text/escaped query tests、`test_failure_error_id_diagnostics_and_request_context_survive_concurrent_requests`、HTTP safe diagnostics/validation tests。公开输出无 secret/内部路径，后端诊断有安全化 chain。 | 详细设计 §11–12、API ID/error/runbook 与 README；M3 持久化写入边界继续独立执行 redaction。 |
| `GLOBAL-C04`、`GLOBAL-C09`、`M2-C02`、`M2-C03`、`M2-C04`、`M2-C08` | `test_completed_lifecycle_json_matches_sse_and_original_objects` 验证 committed 对象身份、timestamp、sequence、request/session ID 与 SSE 一致；状态机 race/原子提交和 event replay 回归通过。 | 详细设计 §6、§12，API lifecycle log 查询方式；Milestone 7 真实 SSE smoke。 |
| `M2-C01`、`M2-C05`、`M2-C06`、`M2-C07`、`M2-C13` | `test_cancelled_late_exception_keeps_diagnostics_without_a_failed_event`、keepalive JSON 不变、headers 后失败关联，及现有 disconnect/cancel/shutdown/quiesced 全量回归。 | API 断线/cancel 排障与设计 §12；Milestone 7 server/CI，M3 持久化遗留处理归属不变。 |
| `M2-C10`、`M2-C11`、`M2-C12` | 幂等 logger 配置、九组日志故障×业务终态测试；现有并发 session/lifespan/task ownership 回归。无新增 task/worker、容量清理、持久化或 fake 业务结果。 | 设计进程级 logger/单 worker 说明；Milestone 7 跨平台验收、Milestone 8 最终 sweep。 |
| `GLOBAL-C01`、`GLOBAL-C02`、`GLOBAL-C10` | 仅新后端路径；无新依赖和 API/schema 变更，既有 architecture/OpenAPI gates 全绿。最终 JSON capture 承接原有日志安全断言。 | 三份 M2 文档和 backend README 同步；中心 register 无新增 deferred contract，Milestone 8 最终承接登记待完成。 |

Milestone 3–5 完成记录中的 JSON formatter、duration_ms 和通用 redaction 待接入项已由本阶段承接。M2 总成功标准与 Milestone 7–8 保持未勾选，不将本阶段日志完成等同于完整 M2 已验收。

## Milestone 7：真实 Server Smoke、API 文档与 CI

### Smoke

- [x] 新增跨平台 Python smoke client/server orchestration。
- [x] 使用真实 Uvicorn socket 验证 create -> SSE replay -> completed。
- [x] 使用 controlled gate 验证 create -> cancel -> cancelled event。
- [x] 验证 terminal stream close。
- [x] smoke 不调用 shell、POSIX signal 或硬编码端口。
- [x] smoke 使用可用 loopback port，并可靠清理 server/task。

### API docs

- [x] 核对 `m2_analysis_session_sse_api.md` 与实际 JSON/OpenAPI。
- [x] 核对 raw SSE frame、Last-Event-ID、204 和 keepalive。
- [x] 核对 success/error examples 和错误码表。
- [x] 核对 JavaScript EventSource/cancel 示例，但不修改 frontend。
- [x] 核对排障 runbook 中所有日志 event name 和 correlation ID。
- [x] 文档明确单 worker、内存态、重启 404 和无旧前端兼容。

### CI

- [x] 更新或新增 M2 workflow，继续在 Ubuntu/macOS/Windows + Python 3.11 执行 editable install。
- [ ] 三平台运行完整 tests（配置已接入，远端结果待运行）。
- [ ] 三平台运行 health smoke 和 M2 session/SSE smoke（配置已接入，远端结果待运行）。
- [x] CI 不依赖系统 IANA timezone database、POSIX-only command 或固定路径。

### Verify

- [x] `python -m pytest backend/tests -q`
- [x] `python backend/scripts/health_smoke.py`
- [x] 运行 M2 session/SSE smoke。
- [x] `git diff --check`

### Milestone 7 实现与本机验收记录（2026-09-08）

本阶段新增真实 Uvicorn session/SSE smoke、完善 API/OpenAPI 文档校验，并接入三平台 CI。开始时工作区干净，基于本地 Milestone 6 提交 `6394353`，全量基线为 566 项通过。只修改新后端、相关 tests、backend README、三份 M2 文档和现有 CI workflow；不增加依赖，不修改业务流程或 HTTP/SSE 数据模型。

#### 实现与验收

- `backend/scripts/session_sse_smoke.py`：运行既有 `create_app` 与 `uvicorn.Server.serve`，持有绑定端口 0 的 loopback socket 并直接交给 server。Startup 用 event 通知，结束设置 `should_exit` 并等待 server/lifespan 清理；不安装 signal handler，不调用 shell 或 subprocess。Client、socket 和 server task 均有显式生命周期，网络读取和整次 smoke 有超时边界。
- Completed 场景使用默认 workflow，断言 201/pending、timezone/resolution/source snapshot、canonical request ID、created/completed 的一致 session ID 和 sequence，以及 terminal EOF。再以 Last-Event-ID=1 精确回放终态，以 Last-Event-ID=2 得到空 body 的 204。
- Cancelled 场景注入 gate workflow，实际收到 created 和 keepalive 后断开并从 sequence 1 重连，确认 disconnect 未设置取消信号。Gate 关闭时调用正式 cancel route，验证 API 返回时 workflow 尚未退出、取消信号已可见、只有一次 cancelled event；重复 cancel 返回相同 snapshot。释放 gate 后再验证 terminal replay/204。Reader 跳过传输 keepalive，避免 cancel response 返回前已缓冲的 comment 导致假失败；不以 sleep 决定 workflow 时序。
- `backend/tests/smoke/test_session_sse_smoke.py` 新增 4 项真实 socket 测试：正常退出、客户端失败和调用 task 被取消时，活动 workflow 被收敛，socket 不再接收请求，无遗留 async task；lifespan startup 失败转换为可报告的启动错误，不让 Uvicorn 的 SystemExit 中止测试进程。
- `.github/workflows/backend-m1.yml` 保留原入口，显示名为 `Backend M1-M2`，三平台 Python 3.11 的 editable install/全量 tests/health smoke 继续保留，并新增 session/SSE smoke。两项 smoke 的步骤都注入不可用 HTTP/HTTPS/ALL proxy 和空 NO_PROXY；客户端使用 `trust_env=False`。Architecture gate 校验 CI 命令、动态 socket、单 worker、无 shell/signal/sleep 和关闭控制。
- API 对照发现非法 UTF-8 JSON body 由框架返回 `400 invalid_request`。`api/http/analysis_sessions.py` 为 POST create 补充既有统一 400 schema，并在 GET events 的 OpenAPI description 引用 streaming 文档；没有改变实际处理逻辑。错误码表和真实请求回归同步覆盖该情况。
- API tests 以实际 route/OpenAPI 对照文档中的路由、request/response/header/media type；错误测试对照 Markdown 中 HTTP/code 配对。新增确定 clock/ID 的真实 POST 示例精确比较；复用 JSON DTO、四种 raw frame、keepalive、cursor/204、日志关联测试。JavaScript EventSource/cancel 示例已按同源新协议、自动 Last-Event-ID、无 body cancel 和 terminal close 语义核对，未修改前端或引入浏览器测试依赖。

#### Verify

使用现有 macOS / Python 3.12.14 环境，从仓库根执行：

```text
backend/.venv/bin/python -m pytest backend/tests/api backend/tests/architecture/test_cross_platform_ci.py -q
173 passed in 0.87s

backend/.venv/bin/python -m pytest backend/tests/smoke -q
4 passed in 1.19s

backend/.venv/bin/python -m pytest backend/tests -q
572 passed in 2.35s

backend/.venv/bin/python backend/scripts/health_smoke.py
{"status":"healthy"}

backend/.venv/bin/python backend/scripts/session_sse_smoke.py
{"completed":"passed","cancelled":"passed"}

git diff --check
```

上述检查均退出 0；真实 socket 测试与 smoke 在允许 loopback bind 的环境执行。最后两项 smoke 同时设置 HTTP_PROXY、HTTPS_PROXY、ALL_PROXY 为 `http://127.0.0.1:1`，NO_PROXY 为空，验证本机请求仍能成功。全量新增 6 项测试；当前异步测试没有遗留任务，两个 smoke 均正常退出。修改后的文档继续由 schema/raw-frame/route/error 对照测试验收。

#### Contract evidence 与剩余验收

Applicable contracts：`GLOBAL-C01`、`GLOBAL-C02`、`GLOBAL-C04`、`GLOBAL-C07`、`GLOBAL-C09`、`GLOBAL-C10`，以及 `M2-C01`–`M2-C13`。按 `MR-G01`–`MR-G14` 记录本阶段证据，中心 register 的硬约束与归属保持不变。

| Contract | 实现与测试证据 | 文档与后续验收 |
|---|---|---|
| `GLOBAL-C04`、`GLOBAL-C09`、`M2-C02`、`M2-C03`、`M2-C04`、`M2-C08` | `completed_smoke/verify_replay` 验证真实 snapshot/event/ID/sequence、created/completed、终态 replay/204/EOF；既有状态转换/日志同一 event 回归。文档创建示例通过真实 POST 精确比对。 | API §3–4、§9–10 与设计 §16.6；三平台实际 CI 结果待取得。 |
| `M2-C01`、`M2-C05`、`M2-C06`、`M2-C07` | `cancelled_smoke` 的真实 disconnect/reconnect/keepalive/cancel/幂等/terminal close，gate 验证 cancel 不等待在途 workflow；晚到输出和竞争 API/application 回归。 | API §4–5、§10，README smoke 入口；远端三平台验收待运行。 |
| `GLOBAL-C07`、`M2-C09` | 真 socket request ID 检查、文档错误 HTTP/code 配对、非法 UTF-8 400 的安全响应/OpenAPI metadata；原日志 redaction、request/session/error correlation 全量通过。 | API 错误表、OpenAPI streaming 引用、runbook 的 `event_sequence` 名称同步。 |
| `M2-C10`、`M2-C11`、`M2-C12`、`M2-C13` | 单 worker Uvicorn、动态 socket、进程内状态及无 fake 业务结果保持；`test_server_cleanup_closes_socket_and_workflow_on_client_exit` 与 startup failure 测试验证 server/lifespan/task 收尾，不伪造 cancel event。 | README 和 API 明确单 worker、内存态、重启丢失；M3 持久化/遗留处理归属不变。 |
| `GLOBAL-C01`、`GLOBAL-C02`、`GLOBAL-C10` | 只使用新后端生产入口与 factory workflow 注入；无新依赖、测试路由或 frontend 改动。既有跨平台 CI 扩展与 architecture gates、完整 M1/M2 回归通过。 | 三份 M2 文档和 README 同步；没有新增 deferred contract，Milestone 8 最终 sweep/承接仍待完成。 |

Milestone 7 的实现与本机验收完成；**远端 Ubuntu/macOS/Windows + Python 3.11 的实际 tests 和 smoke 结果尚未取得**，对应 CI 验收项保持未勾选。本次没有推送或触发远端 workflow，不能把已配置三平台矩阵或本机通过写成三平台 CI 通过。该验收仍归 Milestone 7，不转移为新的 deferred contract；M2 总成功标准和 Milestone 8 总验收保持待完成。

## Milestone 8：Contract Sweep 与接力

### End-to-end sweep

- [x] 从 create request 追踪到 snapshot、runtime、workflow、event、SSE、日志和 cancel。
- [x] 搜索并移除旧 WebSocket 名称、旧字段和临时 fake result（新生产路径中未发现残留）。
- [x] 搜索重复 event/error/session schema，保证只有一个事实模型。
- [x] 确认生产代码没有 test-only behavior switch。
- [x] 确认没有 session_started 公开事件。
- [x] 确认 subscriber disconnect 不触发 cancel。
- [x] 确认 completed/failed/cancelled 每 session 最多一个。
- [x] 确认未实现 history、transcript、query understanding 或 persistence。

### Architecture tests

- [x] Domain 依赖边界继续成立。
- [x] API 只依赖 application coordinator，不直接操作 runtime 或 infrastructure 内部字段。
- [x] application 不依赖 FastAPI request/response 类型。
- [x] no-new-dependency assertion 更新为 M2 仍使用 M1 dependency set。
- [x] 单 worker 限制出现在启动/API 文档，不添加多 worker 配置。

### Deferred register handoff

- [x] 按中心登记表的 `MR-G01`–`MR-G14` 完成本地 MR contract evidence 和文档同步；最终承接仍待三平台 CI。
- [x] M3 接力 `M3-C08`–`M3-C15`：生命周期持久化、幂等创建、cancelled transcript、persistence-error、quiesced/grace cleanup 和 startup orphan handling。
- [x] M4 接力 `M4-C01`–`M4-C08`：final request、date mapper、auto resolution、query/topology events 和 history query 扩展。
- [x] M5 接力 `M5-C09`–`M5-C10`：evidence event 的 payload 容量边界和延后 producer 槽位。
- [x] M6/M7 接力 `M6-C11`–`M6-C13`、`M7-C04`–`M7-C08`：final result/history、agent/tool IDs、真实 cancellation boundaries、topology result 和 concurrency policy。
- [x] M9 接力 `M9-C06`–`M9-C09`：前端重构、最终 API 校对、admin cleanup 和单 worker demo 说明。
- [x] Future 接力 `FUTURE-C09`–`FUTURE-C13`：multi-worker/shared pubsub、retention/backpressure、recovery、security 和 tracing。

### Final verification

- [x] 所有 M2 focused tests 通过。
- [x] 所有 M1 regression tests 通过。
- [x] completed 和 cancel 两条真实 Uvicorn smoke 通过。
- [ ] Linux/macOS/Windows CI 通过（矩阵与命令已接入，实际运行结果待取得）。
- [x] `git status --short` 只包含预期文件。
- [x] `git diff --check` 通过。

### Milestone 8 实现与本机验收记录（2026-09-08）

开始时工作区干净，分支 `codex/m2-analysis-session-sse`，基于本地 Milestone 7 提交 `b3cd6d1`；全量基线为 572 项通过。本阶段完成 M2 内部的最终 contract sweep 和后续接力登记，不实现项目 M8 的 Memory/RAG。只修改新后端启动入口、相关测试、README、三份 M2 文档和中心 deferred register。

#### 实现与范围

- `backend/app/__main__.py:main` 显式使用 `workers=1`。正式入口不能因环境中的 `WEB_CONCURRENCY` 改为多进程；相同入口同时服务 `python -m app` 和 console script。README、API 和设计同步直接 Uvicorn 启动所需的 `--workers 1`。
- `backend/tests/architecture/test_application_dependencies.py` 扫描 API/application 的依赖和 runtime 所有权，覆盖别名、相对导入、直接私有字段及常量属性名的反射访问。生产源码门禁与 13 组正反例均通过；API 可保留 transport 自身的私有字段，不能访问 coordinator 的 session map/lookup/runtime。
- 既有 domain 门禁保持；依赖集合断言命名明确为 M2 保持 M1 dependency set，断言内容不变。没有新增依赖、通用框架或 HTTP/schema 字段。
- 新生产代码与 scripts 的旧 WebSocket/字段/公开 `session_started`/fake outcome 开关搜索无匹配。模型定义只包含权威 application snapshot、configuration、PublicError、CorrelationContext 和 typed LifecycleEvent；API response 继承 snapshot，HTTP error 仅扩展必要的关联字段和 envelope。
- API/application 的 history/transcript/persistence/TTL/LRU/`Any` 搜索仅命中预留状态名 `completed_with_persistence_error`，M2 不产生该终态或对应 payload。既有 generic ports 保留延后类型参数，不构成持久化实现。未发现本阶段改动导致的孤立方法；设计明确要求的 `get_snapshot` 继续作为 application immutable read 入口。

#### 端到端追踪

下表路径均相对于仓库根；每一步继续使用上一步的规范化事实，不再创建另一套语义模型。

| 链路 | 真实入口 | 保留的身份、顺序与失败语义 |
|---|---|---|
| HTTP 输入与配置 | `backend/app/api/schemas/analysis_sessions.py:CreateAnalysisSessionRequest/to_configuration_snapshot`；`backend/app/api/http/analysis_sessions.py:create_session` | query 在边界校验；省略粒度得到 12h/default，显式粒度得到 frontend；IANA timezone 固化，拒绝 null/auto/额外字段，不回显 query。 |
| 创建与所有权 | `backend/app/application/analysis_sessions/coordinator.py:create_session`；`runtime.py:SessionRuntime` | pending snapshot 与 sequence 1 created event 同一时间；map 临界区登记 workflow task 与 session 后返回捕获的 pending snapshot。runtime 不交给 API。 |
| Workflow 与状态 | `coordinator.py:_run_workflow/_transition`；`workflow.py:WorkflowContext/CancellationToken` | 工作流拿 immutable snapshot 和 token；每次转换只读一次 ClockPort，在同一 condition 内提交 snapshot/event 并通知；running 无公开事件，终态竞争最多一个胜出者。 |
| Event 与 replay | `models.py:LifecycleEvent`；`coordinator.py:subscribe/_events` | 类型化、版本化事件只提交一次；从保留列表派生 sequence；同一 condition 处理 replay/wait/timeout，独立 subscriber cursor，无空隙或重复。 |
| HTTP/SSE 输出 | `backend/app/api/sse/encoder.py:encode_event`；`response.py:create_event_response` | 纯序列化原 event，精确 id/event/data framing；发送前检查 400/404/409，终态已消费返回 204。keepalive 无 ID；断线/写失败只关闭该订阅。 |
| 日志与错误 | `backend/app/api/http/request_context.py`、`errors.py`；`backend/app/infrastructure/observability/json_logging.py` | 服务端 request ID 与 session/error ID 串联，event/error 原对象复用；公开错误安全，日志记录耗时和脱敏诊断；日志失败不回滚状态，不在 SSE headers 后追加 HTTP JSON。 |
| Cancel 与收尾 | `coordinator.py:cancel_session/_transition/_run_workflow/shutdown`；`backend/app/main.py:create_app` lifespan | cancel 原子提交 cancelled 并立即返回，重复调用幂等；晚到结果/异常不提交新终态或传播下游。shutdown 单独发 stop signal，await task/quiesced，不伪造 cancelled。 |
| 持久化与下游装配 | M2 不接入；见下方接力表 | 没有 history、transcript、真实业务 result、query understanding 或持久化双写；不以日志或内存 map 冒充 durable truth。 |

#### Verify

单 worker 回归先在未固定 worker 的正式入口执行：两例均失败，`WEB_CONCURRENCY=4` 产生 workers=4，非法整数使真实 Uvicorn Config 抛出 ValueError。固定入口后两例通过；没有放宽断言或增加生产测试开关。

从仓库根执行，均使用现有 `backend/.venv`，未升级依赖：

```text
backend/.venv/bin/python -m pytest backend/tests/architecture backend/tests/api/test_health.py -q
71 passed in 0.71s

backend/.venv/bin/python -m pytest backend/tests/application backend/tests/api backend/tests/observability -q
364 passed in 1.07s

backend/.venv/bin/python -m pytest backend/tests -q
588 passed in 2.25s

backend/.venv/bin/python -m pytest backend/tests/api -k 'document or openapi' -q
4 passed, 167 deselected in 1.70s

backend/.venv/bin/python backend/scripts/health_smoke.py
{"status":"healthy"}

backend/.venv/bin/python backend/scripts/session_sse_smoke.py
{"completed":"passed","cancelled":"passed"}

git diff --check
```

最终全量新增 16 项测试。两项 smoke 都设置 HTTP_PROXY、HTTPS_PROXY、ALL_PROXY 为 `http://127.0.0.1:1` 且 NO_PROXY 为空；health smoke 另设置 `WEB_CONCURRENCY=not-an-integer`，验证子进程正式 `python -m app` 启动不受影响；session/SSE smoke 设置 `WEB_CONCURRENCY=4`，继续用单 worker 真实 server 验证 completed/cancel。上述检查均退出 0，server/task/socket 正常清理。测试命令中的环境变量可通过各平台的进程环境注入，不要求 POSIX shell。

#### M2 contract evidence

Applicable contracts：`GLOBAL-C01`、`GLOBAL-C02`、`GLOBAL-C04`、`GLOBAL-C07`、`GLOBAL-C09`、`GLOBAL-C10`，以及 `M2-C01`–`M2-C13`。下表为本地实现证据，**整体验收仍待三平台实际 CI**。测试名可在 `backend/tests` 定位，均包含在上方完整回归；文档列使用设计/API 的对应章节。

| Contract | 实现位置 | 测试证据 | 文档 |
|---|---|---|---|
| `GLOBAL-C01`、`GLOBAL-C02` | `backend/app/main.py:create_app`、API/application 新路径；无旧系统导入或兼容分支 | `test_openapi_describes_only_the_supported_routes_and_actual_envelopes`、`test_api_and_application_respect_dependency_and_runtime_boundaries`；新生产路径边界搜索 | 设计 §2、§14；API 开篇、§1 |
| `GLOBAL-C04` | `CreateAnalysisSessionRequest.to_configuration_snapshot`、`AnalysisConfigurationSnapshot`、`SessionCoordinator._transition` | `test_create_exact_pending_snapshot_and_configuration`、`test_configuration_does_not_follow_later_settings_changes`、`test_lifecycle_commits_one_timestamp_and_event_per_public_transition` | 设计 §4–5；API §3 |
| `GLOBAL-C07`、`M2-C09` | `api/http/errors.py`、`request_context.py`；`infrastructure/observability/json_logging.py`、`redaction.py` | `test_failure_error_id_diagnostics_and_request_context_survive_concurrent_requests`、`test_validation_and_unknown_paths_never_echo_inputs_in_json_logs`、`test_error_envelopes_reject_arbitrary_details_and_tracebacks` | 设计 §11–12；API §2、§6、§8 |
| `GLOBAL-C09`、`M2-C08` | `application/analysis_sessions/models.py:LifecycleEvent/PublicError/CorrelationContext`、API schema 继承及 SSE/log 序列化 | `test_completed_lifecycle_json_matches_sse_and_original_objects`、`test_success_response_reuses_public_snapshot_without_query`、`test_documented_raw_frames_match_the_encoder` | 设计 §4、§7、§12；API §3–4、§9 |
| `GLOBAL-C10` | 中心 register §13 待验收索引与本记录接力表 | `MR-G01`–`MR-G14` 对照；逐项检查原 contract ID、真实入口、约束和未来验收，未改归属或新增 ID | 设计 §18；本记录与中心 register |
| `M2-C01` | `api/sse/response.py`、`SessionCoordinator.subscribe/_events/cancel_session` | `test_stream_disconnect_or_write_failure_does_not_cancel_workflow`、`test_multiple_subscribers_share_events_with_independent_cursors`；`cancelled_smoke` | 设计 §8–9；API §4–5 |
| `M2-C02` | `SessionCoordinator.subscribe/_events`、`api/sse/encoder.py` | `test_replay_to_wait_has_no_gap_or_duplicate`、`test_published_event_wins_when_timeout_fires_while_waiting_for_the_lock`、`test_terminal_replay_and_consumed_cursor_response`；`verify_replay` | 设计 §7–8；API §4 |
| `M2-C03`、`M2-C04` | `SessionCoordinator._transition/_terminal_event/_log_transition` | `test_terminal_race_has_one_winner_and_one_terminal_event`、`test_failed_validation_never_partially_commits_or_consumes_event_sequence`、`test_logging_runs_after_unlock_and_cannot_undo_committed_state` | 设计 §6–7；API §4–5 |
| `M2-C05`、`M2-C06` | `SessionCoordinator.cancel_session/_run_workflow`、`CancellationToken` | `test_cancel_returns_before_quiescence_and_suppresses_late_output`、`test_real_workflow_and_cancel_race_commit_one_terminal`、`test_cancel_is_immediate_idempotent_and_replays_one_terminal`；`cancelled_smoke` | 设计 §9–10；API §5、§8.4 |
| `M2-C07` | `SessionCoordinator._events`、`api/sse/encoder.py` 的 comment frame | `test_keepalive_is_transport_only_and_does_not_change_replay_or_logs`、`test_stream_keepalive_does_not_block_cancel_or_mix_request_contexts` | 设计 §8.4；API §4.6 |
| `M2-C10` | `backend/app/__main__.py:main`、`SessionRuntime` per-session 资源 | `test_main_entrypoint_enforces_one_worker_despite_environment`、`test_concurrent_workflows_have_independent_cancellation_and_completion`；带 worker 环境的 smoke | 设计 §13.1–2、§16.7；README 启动；API §1 |
| `M2-C11` | `SessionCoordinator` map/event list、`create_app` lifespan；无后台清理入口 | `test_terminal_replay_and_consumed_cursor_response`、`test_lifespan_creates_fresh_runtime_on_each_start_without_import_time_tasks` | 设计 §13.3；API §1、§8.6 |
| `M2-C12` | `DefaultFakeWorkflow`、`SessionCompletedPayload`，仅生命周期输出 | `test_default_workflow_completes_and_idempotency_key_is_not_reused`、`test_create_rejects_fields_outside_m2`；`completed_smoke` | 设计 §2、§10；API §1、§4.3 |
| `M2-C13` | `SessionCoordinator.shutdown`、`create_app` lifespan | `test_shutdown_signals_cooperative_workflow_without_fabricating_cancel`、`test_shutdown_handles_multiple_tasks_including_cancelled_but_not_quiesced`、`test_server_cleanup_closes_socket_and_workflow_on_client_exit` | 设计 §13.4；API §5、§8.4 |

#### MR 门禁对照

本记录作为本地 MR 的可审阅 evidence；没有发布远端 MR 或触发远端 workflow。

| 门禁 | 本阶段证据与状态 |
|---|---|
| `MR-G01`、`MR-G02` | 上方明确全部适用 ID、代码入口、具体测试名和文档章节；历史阶段证据继续保留。 |
| `MR-G03`、`MR-G04` | 遵循已确认的 M2 设计；单 worker 入口、回归、README、API/设计/TODO 同步。HTTP/SSE shape 不变，现有 schema/文档/真实路由对照测试继续通过。 |
| `MR-G05`、`MR-G06` | 修复启动契约遗漏；没有弱化断言。已逐步追踪输入到 cancel/shutdown，并搜索过时事件/字段、重复模型、未知 DTO 和越界依赖。 |
| `MR-G07`、`MR-G08` | 测试调用正式 main、create_app、HTTP/SSE/coordinator；fake 只通过 factory 控制时序。成功/失败/重复/竞态由现有 clock/gate 回归覆盖，真实 server 两条主链路通过。 |
| `MR-G09`、`MR-G10` | secret/error/log correlation 回归全绿；范围只在新后端与新文档，开始时无用户未提交改动，交付 status 只含预期文件。 |
| `MR-G11` | 既有三平台架构/CI 门禁和无系统 tzdata 测试通过，入口采用 Python/Uvicorn API；实际 Ubuntu/macOS/Windows + Python 3.11 CI 尚待验收。 |
| `MR-G12` | 本地实现、focused/regression/smoke 和待验收索引齐全；完整跨平台结果尚缺，不向中心最终承接表写入 `implemented` 或 `partial`。 |
| `MR-G13`、`MR-G14` | 生产代码仅固定启动 worker，未新增框架或依赖；原 deferred ID、归属和硬约束保持，未发现需要新增 ID 的事项。 |

#### 后续接力入口与验收

新增设计接力（尚未实现）：[M3 D26](../M3/m3_transcript_history_design.md#d26日志保留-query-正文仅对其中凭据脱敏) / `M3-C19` / `M3-A11` 将日志策略改为保留业务 query、仅脱敏其中凭据。M3 应调整 `redaction.py`、formatter 字段、HTTP/workflow 诊断私有值及对应测试；本文件此前关于隐藏 query 的通过记录保留为当时实现证据，不再作为新设计的禁止规则。凭据保护和公开错误安全边界继续验收。

以下“入口”指当前可读取、复用或直接演进的真实符号，不代表未来功能已实现。各 ID 的完整硬约束仍以中心 register 为准；后续实现必须保留 M2 基线，并在所属 Milestone 补齐 durable/业务能力的测试。

| 归属与 ID | 当前接力入口 | 必须保留的约束与可观察验收 |
|---|---|---|
| M3：`M3-C08`、`M3-C09`（关联 `M3-C01`–`M3-C07`） | `SessionCoordinator.create_session/_transition`、`models.py`、`CreateAnalysisSessionRequest`、`backend/app/ports/transcript_store.py` | 存储 request/session/原 LifecycleEvent/errors；存在时才保存 versioned final result。durable append 后重启读取仍保留 schema version、sequence、原时间、configuration；同 key + 完全相同的原始请求体跨重启返回原 session，不同请求体返回 409；按 M3 D12 原样比较，不额外规范化。大内容 preview/reference、写前脱敏及 history 无假字段一并验收。 |
| M3：`M3-C10`、`M3-C11` | `SessionCoordinator._run_workflow/_workflow_failed/_transition`、`SessionCompletedPayload`、`PublicError` | cancelled 在途产物只追加 transcript，不进入 SSE/downstream，不增加 after-cancel 字段；按 D38，成功分析结束时固定此前必需写入范围，交付窗口到期仍未保存完整则提交 completed_with_persistence_error 与 session_completed 安全 warning/error_id。终态自身/后续收尾保存失败通过当前读取返回警告，保持原终态并补写；测试唯一终态、取消即时响应、写失败仍能交付结果且不误报 session_failed。 |
| M3：`M3-C20`、`M3-A12` | `SessionCoordinator`、`SessionRuntime`、新增 M3 持久化写入器和保存恢复 route | 按 D39–D41 实现无请求体 POST /api/analysis/sessions/{session_id}/persistence/retry，自动补写耗尽后复用原写入器开启新一轮；执行/退避中重复触发不增加并发或重置预算。202/200 与 409/404/503/422 按 D41 验收，HTTP 断开不取消已受理补写，原事实与终态保持不变，内存丢失不伪造恢复。接口和验收待 M3 实现，按钮及不自动重发由 M9-C11/M9-A08 承接。 |
| M3：`M3-C12`–`M3-C15` | `SessionRuntime.workflow_quiesced`、`SessionCoordinator` session map、`shutdown/subscribe`、`create_app` lifespan | 本 session 所有适用 durable write 成功回调 + quiesced + grace 到期才清理；仅入队/建 task/live buffer 不算确认。分别验证未确认、失败、未收敛、未到期、幂等重试及 cleanup 失败不改终态；启动遗留 active 归档 failed/service_interrupted；重启后历史 timeline 不伪装可续跑 active stream。 |
| M4：`M4-C01`–`M4-C08` | `CreateAnalysisSessionRequest`、`AnalysisConfigurationSnapshot`、`WorkflowContext`、`backend/app/domain/time/`、`models.py:LifecycleEvent` | 直接替换阶段 request，定型 AnalysisContext；包含整天结束日期按 session timezone 转 UTC，DST/auto 真正规则有测试；使用 ParameterKey，真实解析 location/COMID/mode/topology；query_understood/topology_ready 共享 envelope/sequence/replay。history 从持久化版本化 context 投影，查询时不重算。 |
| M5：`M5-C09`、`M5-C10`；producer 归 M7 / 项目 M8 | `models.py:LifecycleEvent`、`api/sse/encoder.py`、`backend/app/ports/event_publisher.py` | 大 tool/evidence 仅 preview+reference，不把无界 raw data 放入 replay buffer；类型、容量及完整内容引用可测试。upstream-result/RAG tool 槽位明确 unavailable，真实 producer 分别由 M7 和项目 M8 接入，不能用生产 fake 伪装可用。 |
| M6：`M6-C11`–`M6-C13` | `SessionCompletedPayload`、`CorrelationContext`、`json_logging.py`，及届时 M3 history/result projection | 用真实 versioned inference result 替换空业务 payload，SSE/log/transcript 保留同一 result identity；稳定 agent/tool/operation ID 与 stage 以 session 为根关联。history summary/confidence 来自持久化 result，不重新推理，不保留 fake 兼容结构。 |
| M7：`M7-C04`–`M7-C08` | `AnalysisWorkflow`、`WorkflowContext.cancellation`、`SessionCoordinator._run_workflow`、`SessionRuntime.workflow_quiesced` | 逐个真实 agent/tool/阶段边界测试取消，不启动新工作或传播晚到结果；无法强杀的调用只审计到 cancelled transcript/log。记录 cancel_accepted/quiesced，cleanup 等待收敛；按真实负载明确并发/排队策略；最终 completed 收敛为一套拓扑 result 语义。 |
| M9：`M9-C06`–`M9-C09` | `create_app`、正式 HTTP/SSE 路由、`backend/scripts/session_sse_smoke.py`、`backend/app/__main__.py`，及届时 M3 cleanup | admin 按 ownership/幂等规则清 history/transcript/index/memory/cache，测试 API keys 保留；最终 OpenAPI、SSE、独立重构的前端、smoke 和启动说明一致；不读取或迁移旧前端；demo 仍明确单 worker。 |
| Future：`FUTURE-C09`–`FUTURE-C13` | `SessionCoordinator` map/condition/cancel、`subscribe/_events`、lifespan、`RequestContextMiddleware`、`CorrelationContext` | 共享 store/pubsub 与跨进程 cancel 必须跨 worker 验证；retention/慢订阅者 backpressure/磁盘告警须有容量与失败测试；drain/resume/crash recovery 不损坏顺序或身份；认证/授权/租户/限流/审计覆盖隔离；tracing/metrics 保留 session/request/error ID。 |

中心 register §13 已增加本地证据索引及本表链接；原 contract、归属和最终状态规则不变。**剩余验收只有已配置矩阵的 Ubuntu/macOS/Windows + Python 3.11 完整 tests 与两项 smoke 的实际结果**；取得结果后更新 Milestone 7/8 的 CI 勾选、M2 成功标准及中心最终承接记录。

### Milestone 8 审查修复记录（2026-09-08）

基于本地提交 `48decb0`，开始时工作区干净。本阶段修复正式 Uvicorn 启动的两处审查问题：活动 SSE 阻止 lifespan shutdown，以及默认 access log 原样记录完整 URL。新增两项真实 server 回归先在原入口失败：保持订阅时 5 秒内未退出，URL 中的合成凭据出现在 stdout；使用修复后的正式入口配置后通过。

`backend/app/__main__.py:main` 固定 `access_log=False` 和 `timeout_graceful_shutdown=2`。Uvicorn 先有界等待 HTTP/SSE，再执行既有 application/workflow 清理，关闭过程不生成 cancelled 终态；HTTP 访问保留已有安全 JSON 日志。README 的直接启动命令与 API/设计同步三个必需选项。未改 HTTP/SSE schema、状态机、依赖、前端或后续 Milestone 归属。

`backend/scripts/session_sse_smoke.py:SmokeServer` 接收 Uvicorn Config，复用已有 readiness、无 signal 和异常收尾控制。`backend/tests/smoke/test_server_entrypoint.py` 调用正式 `main()`，捕获其参数并用真实 `app.main:app` 入口启动 server，不重写访问日志与关闭超时；通过 app factory 注入协作式 workflow，保持 SSE 开启直到 server 退出，同时检查完整 stdout/stderr。测试结束恢复 logger 配置并关闭 client/server/socket，关闭回归还检查没有残留 task。

Applicable contracts：`GLOBAL-C07`、`GLOBAL-C09`、`M2-C01`、`M2-C09`、`M2-C10`、`M2-C13`。其余 M2 基线由全量回归继续验证。

| Contract | 实现与测试证据 | 文档 |
|---|---|---|
| `GLOBAL-C07`、`GLOBAL-C09`、`M2-C09` | 正式入口关闭默认 access log，复用已有安全 HTTP 日志；`test_entrypoint_logs_exclude_url_credentials_and_private_query` 验证完整输出无 URL/私有 query，200/404 的 route/status/request ID 仍存在。 | README Run/Configuration；设计 §12.3、§16.7；API §8、§10 |
| `M2-C01`、`M2-C13` | 正式入口有界 drain，再进入已有 lifespan；`test_entrypoint_shutdown_finishes_with_an_open_sse_subscription` 验证客户端未主动断线时 server 退出、workflow quiesced、无 cancelled event；现有 cancel/disconnect/shutdown 回归全绿。 | README Run；设计 §13.4、§16.7；API §5、§10 |
| `M2-C10` | `workers=1` 保留，既有环境变量单 worker 回归及两项真实 smoke 通过。 | README 启动命令；设计 §13.1；API §1 |

从仓库根执行：

```text
backend/.venv/bin/python -m pytest backend/tests/smoke backend/tests/api/test_health.py backend/tests/architecture -q --tb=short
77 passed in 4.71s

backend/.venv/bin/python -m pytest backend/tests -q
590 passed in 4.93s

backend/.venv/bin/python backend/scripts/health_smoke.py
{"status":"healthy"}

backend/.venv/bin/python backend/scripts/session_sse_smoke.py
{"completed":"passed","cancelled":"passed"}
```

两项 smoke 均注入不可用 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY 和空 NO_PROXY；上述验证均退出 0。现有 CI 的完整 tests 会执行两项新增回归，不需要新增依赖或 CI 步骤。按 `MR-G04`、`MR-G07`、`MR-G08`、`MR-G09`、`MR-G11`、`MR-G12` 同步入口、真实 server 验收和文档；三平台远端实际结果仍待取得，不把本机修复通过标记为最终已承接。

### Milestone 8 日志边界修复记录（2026-09-09）

基于本地提交 `b9cd8f0`，开始时工作区干净。本阶段完成 headers 后异常传播与应用 trace/Uvicorn trace 隔离的修复。仅修改新后端 middleware、正式启动入口、相关测试、README、M2 文档与中心 register；没有新增依赖、日志框架、HTTP/SSE 字段或配置项。

- `backend/app/api/http/request_context.py`：原异常先交给已有 `log_http` 输出脱敏诊断，随后抛出固定 `HTTP response stream aborted; request_id=...` RuntimeError，使用 `from None` 抑制原异常链的标准 traceback 输出。Uvicorn 仍关闭不完整响应，`aclosing` 仍释放订阅，不追加 JSON 或修改 session；继续使用 `except Exception`，不改变 CancelledError/shutdown 语义。
- `backend/app/__main__.py`：只有 `trace` 映射为 Uvicorn `debug`，其他级别不变；应用仍使用原 `AppSettings.log_level`。已有单 worker、禁用 access log、2 秒关闭等待保留。直接 Uvicorn 启动文档显式使用安全服务器级别，应用 trace 单独设置。
- `backend/tests/smoke/test_server_entrypoint.py`：夹具接受 info/trace 参数并恢复 `uvicorn.asgi` logger 状态；真实日志回归检查完整 stdout/stderr，涵盖合成 Authorization、Cookie、URL query 和未知路径。新增一次性 encoder 异常及 cause chain 测试，验证异常后连接中断、订阅 generator 关闭、session 保持 completed、无取消信号、重连精确回放终态及 204，最终无残留 task；JSON 保留原异常类型、脱敏诊断和 request/session ID。
- `backend/tests/api/test_analysis_sessions.py`：更新 headers 后异常及写失败的内部异常预期，验证安全消息、同一 request ID、`__suppress_context__` 与空 cause；保留原有无额外 HTTP JSON、session 终态和日志断言，移除不再使用的 ClientDisconnect import。

新增回归先在修复前运行：info 下已有 URL 日志保护通过；trace URL 检查及 info/trace 的原异常泄漏检查共 3 项失败，完整服务输出中可见合成凭据。当前 Uvicorn 已对 headers 使用占位符，但仍会在 trace scope 中输出 query；headers 不泄漏继续作为回归约束。修复后全部通过。

Applicable contracts：`GLOBAL-C07`、`GLOBAL-C09`、`M2-C01`、`M2-C08`、`M2-C09`、`M2-C13`。

| Contract | 实现与测试证据 | 文档 |
|---|---|---|
| `GLOBAL-C07`、`M2-C09` | 正式入口级别映射及 middleware 安全异常；`test_entrypoint_logs_exclude_url_credentials_and_private_query` 与 `test_stream_failure_is_safe_in_full_server_output_and_can_replay` 均运行 info/trace，完整输出无合成凭据，原诊断与关联 ID 保留，应用 trace 可输出。 | README Configuration；设计 §12.3、§16.7；API §8、§10 |
| `GLOBAL-C09`、`M2-C08` | 继续使用原 lifecycle event、HTTP/SSE envelope 和 JSON formatter；真实 stream 失败后从 sequence 1 重连仍得到同 session 的 sequence 2 completed，再重连得到 204。 | 设计 §11.3、§12.3；API §4、§8.3 |
| `M2-C01`、`M2-C13` | `test_stream_disconnect_or_write_failure_does_not_cancel_workflow`、真实失败/replay 回归及既有 open-SSE shutdown 回归全绿；取消/关闭信号与业务终态语义保持。 | 设计 §13.4；API §5、§8.3 |

从仓库根执行：

```text
backend/.venv/bin/python -m pytest backend/tests/smoke/test_server_entrypoint.py backend/tests/api backend/tests/observability -q --tb=short
240 passed in 4.38s

backend/.venv/bin/python -m pytest backend/tests -q
593 passed in 5.87s

backend/.venv/bin/python backend/scripts/health_smoke.py
{"status":"healthy"}

backend/.venv/bin/python backend/scripts/session_sse_smoke.py
{"completed":"passed","cancelled":"passed"}
```

两项 smoke 均注入不可用 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY 和空 NO_PROXY，均退出 0；health smoke 子进程按脚本使用 info，trace 行为由新增的正式配置服务器回归验收。按 `MR-G04`、`MR-G06`–`MR-G09` 同步调用点、测试预期、真实服务器日志、续传/清理和文档。没有新增 deferred contract；现有三平台 CI 配置会运行这些测试，远端实际结果仍待取得，中心最终承接状态保持未完成。

### Milestone 8 跨平台 smoke 修复记录（2026-09-09）

基于已推送提交 `73ad133`，开始时工作区干净。用户提供的 Windows + Python 3.11.9 CI 输出为 `3 failed, 589 passed, 1 skipped`：正常退出、客户端失败、调用 task 被取消三种场景均在关闭后的连接探测失败，预期 ConnectError，实际为 ConnectTimeout；之前的 workflow 退出与无残留任务断言已通过。Python 3.11 的 eager task factory 用例按能力检测跳过。修复仅涉及 smoke 测试及文档，不修改生产代码、超时配置、依赖、CI 矩阵或 HTTP/SSE 契约。

- `backend/tests/smoke/conftest.py`：观察真实 SmokeServer 的 task 和传入 socket；所有 smoke 测试结束时验证 descriptor 为 -1、asyncio listener 不再监听且 socket 集合为空、server task 已结束且未被强制取消、Uvicorn 连接及请求 task 集合为空。另提供客户端断连表现注入，仅在真实 SSE 读取已经中断后将错误映射为 ReadError，不提前伪造断连。
- `test_server_cleanup_closes_socket_and_workflow_on_client_exit`：三种退出场景分别运行真实连接探测、注入 ConnectError、注入 ConnectTimeout；关闭探测仅接受这两类连接建立失败，保留 workflow 和 task 清理断言。
- `test_entrypoint_shutdown_finishes_with_an_open_sse_subscription` 与 `test_stream_failure_is_safe_in_full_server_output_and_can_replay`：覆盖原生中断与连接重置，接受 RemoteProtocolError 或 ReadError。读取超时、额外事件、状态错误、订阅未释放、回放错误和日志泄漏仍导致失败。

同类检查覆盖 backend tests/scripts 中的连接异常、SSE 读取、计时/等待、进程控制、动态端口、路径和平台分支。Health smoke 已捕获连接错误及超时，并使用 monotonic deadline、sys.executable、subprocess 生命周期和显式 UTF-8；其余相关时序由 event/gate 控制，未发现需要修改的同类断言。

回归先保持旧异常预期运行：关闭测试的连接超时注入得到 `3 failed, 7 passed`；SSE 连接重置注入得到 `3 failed, 5 deselected`。两组失败均由预期异常类型过窄导致，新增资源清理检查通过。修正预期后，从仓库根执行：

```text
backend/.venv/bin/python -m pytest backend/tests/smoke backend/tests/architecture/test_cross_platform_ci.py -q --tb=short
22 passed in 7.67s

backend/.venv/bin/python -m pytest backend/tests -q
602 passed in 10.39s

backend/.venv/bin/python backend/scripts/health_smoke.py
{"status":"healthy"}

backend/.venv/bin/python backend/scripts/session_sse_smoke.py
{"completed":"passed","cancelled":"passed"}
```

两项 smoke 均注入不可用 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY 和空 NO_PROXY。上述结果来自 macOS + Python 3.12 本机，异常注入不等同于 Windows 原生验证；修复后的三平台 CI 仍需通过。

Applicable contracts：`GLOBAL-C07`、`M2-C01`、`M2-C09`、`M2-C13`；相关门禁为 `MR-G05`、`MR-G07`、`MR-G08`、`MR-G11`、`MR-G12`。

| Contract | 实现与测试证据 | 文档 |
|---|---|---|
| `M2-C13` | `check_server_cleanup` 直接检查真实 server/task/socket；三种退出场景及 startup 失败保持资源释放；正式入口活动 SSE shutdown 不伪造取消事件。 | 设计 §16.6；API §10；本记录 |
| `M2-C01` | 原生中断与连接重置下保持 completed、取消信号未触发、订阅已释放，重连精确回放终态并返回 204。 | 设计 §16.7；API §4、§10 |
| `GLOBAL-C07`、`M2-C09` | info/trace 的真实流失败回归继续验证完整 stdout/stderr 脱敏、安全服务器异常及 request/session ID。 | 设计 §16.7；API §8、§10 |

没有新增 deferred contract，未将本机通过写成三平台 CI 通过；中心登记最终承接仍待完成。

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
