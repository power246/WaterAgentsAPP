# M2 Analysis Session + SSE Shell Implementation TODO

生成日期：2026-09-08

本清单用于执行 [M2 详细设计](m2_analysis_session_sse_design.md)。实现必须同时维护 [M2 API 与排障指南](m2_analysis_session_sse_api.md)，不能让代码、OpenAPI、SSE 示例和错误码漂移。

## 执行原则

- [ ] 只修改新后端 `backend/`、M2 文档、必要的上层新架构文档，以及 Milestone 7 所需的 CI workflow。
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
