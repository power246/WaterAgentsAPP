# M3 Transcript Store + History APIs Implementation TODO

更新日期：2026-09-14

本清单执行 [M3 实施详细设计](m3_transcript_history_implementation_design.md)，同时维护 [API 与搭建排障指南](m3_transcript_history_api.md)、父目录 [前端接入约定](../frontend_integration_contracts.md) 和 [中心验收契约](../deferred_milestone_contracts.md#5-m3transcript-store--history-apis)。[D01–D86 决策记录](m3_transcript_history_design.md) 保留已确认依据。

**当前状态：Milestone 0–10 的首轮本机实现与验收已完成，但 2026-09-14 分支全量审查确认了安全、状态收敛、持久化核实、读取和验收契约缺口；相关 Milestone 已追加未勾选回补项，因此当前整体状态恢复为 `partial`，中心最终承接暂不登记 `implemented`。** 此外，Milestone 2/9 的 Ubuntu/macOS/Windows 实际 CI 与跨系统迁移结果仍为 partial。十一个阶段的历史命令、环境和影响面记录保留为当时证据，不替代新增回补项的重新验证；三平台 PostgreSQL CI 入口只有在远端实际运行后才能补为通过，不能由 macOS 本机结果代替。

## 阶段与设计导航

执行某个阶段时，先从下表进入对应设计，再按复选项末尾的链接定位具体规则。`设计 §…` 指实施详细设计，`API §…` 指接口与搭建指南；原 D01–D86 用于追溯决策，不要求每项任务重新通读。工作、Tests 和 Verify 均有依据链接，命令是否通过仍须实际验证。

| 实施阶段 | 设计定位 | 接口、配置或接力依据 |
|---|---|---|
| [Milestone 0：基线、适用契约与工作区](#milestone-0基线适用契约与工作区) | [设计 §1.2](m3_transcript_history_implementation_design.md#12-后续阶段承接)；[设计 §2](m3_transcript_history_implementation_design.md#2-当前代码与目标模块) | [M2 接力与验收](../M2/m2_analysis_session_sse_implementation_todo.md#后续接力入口与验收)；[契约 §2](../deferred_milestone_contracts.md#2-跨-milestone-硬约束) |
| [Milestone 1：Run / Agent session 模型和统一状态入口](#milestone-1run--agent-session-模型和统一状态入口) | [设计 §3.1](m3_transcript_history_implementation_design.md#31-身份与顺序)；[设计 §3.2](m3_transcript_history_implementation_design.md#32-当前状态)；[设计 §3.3](m3_transcript_history_implementation_design.md#33-时间) | [API §3.2](m3_transcript_history_api.md#32-runsummary-与-sessionsummary)；[设计 §4.2](m3_transcript_history_implementation_design.md#42-analysis_sessions) |
| [Milestone 2：PostgreSQL、四表 schema 与可复现环境](#milestone-2postgresql四表-schema-与可复现环境) | [设计 §4](m3_transcript_history_implementation_design.md#4-postgresql-schema)；[设计 §10.1](m3_transcript_history_implementation_design.md#101-技术选择)；[设计 §10.2](m3_transcript_history_implementation_design.md#102-配置与命令) | [API §8.1](m3_transcript_history_api.md#81-环境变量)；[API §8.2](m3_transcript_history_api.md#82-本地-compose)；[API §8.3](m3_transcript_history_api.md#83-已有数据库与升级) |
| [Milestone 3：Typed content、单份正文、引用与安全入口](#milestone-3typed-content单份正文引用与安全入口) | [设计 §5](m3_transcript_history_implementation_design.md#5-typed-content-与读取表示)；[设计 §9.2](m3_transcript_history_implementation_design.md#92-凭据边界) | [API §3.3](m3_transcript_history_api.md#33-transcriptitemview)；[API §4.4](m3_transcript_history_api.md#44-全文) |
| [Milestone 4：创建原子事务、幂等和所有权收敛](#milestone-4创建原子事务幂等和所有权收敛) | [设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)；[设计 §6.3](m3_transcript_history_implementation_design.md#63-原子提交超时与核实) | [API §2](m3_transcript_history_api.md#2-创建与幂等) |
| [Milestone 5：串行写入、确认集合与有限补写](#milestone-5串行写入确认集合与有限补写) | [设计 §6.2](m3_transcript_history_implementation_design.md#62-runwriter)；[设计 §6.4](m3_transcript_history_implementation_design.md#64-重试和交付窗口)；[设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理) | [API §3.1](m3_transcript_history_api.md#31-来源与保存情况)；[API §6.2](m3_transcript_history_api.md#62-persistenceretry) |
| [Milestone 6：统一读取、历史分页与 HTTP 契约](#milestone-6统一读取历史分页与-http-契约) | [设计 §7](m3_transcript_history_implementation_design.md#7-统一读取与历史查询) | [API §3](m3_transcript_history_api.md#3-当前与历史的共同返回模型)；[API §4](m3_transcript_history_api.md#4-get-读取接口)；[API §7](m3_transcript_history_api.md#7-错误与排查字段) |
| [Milestone 7：两层 SSE、消息生命周期与续传](#milestone-7两层-sse消息生命周期与续传) | [设计 §8](m3_transcript_history_implementation_design.md#8-两层-sse-与消息装配) | [API §5](m3_transcript_history_api.md#5-两层-sse) |
| [Milestone 8：取消、遗留修复、关闭与 live 清理](#milestone-8取消遗留修复关闭与-live-清理) | [设计 §9.1](m3_transcript_history_implementation_design.md#91-原消息与晚到审计)；[设计 §10.3](m3_transcript_history_implementation_design.md#103-启动恢复和关闭)；[设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理) | [API §6.1](m3_transcript_history_api.md#61-cancel)；[API §5.4](m3_transcript_history_api.md#54-游标与结束) |
| [Milestone 9：完整集成、三平台 smoke 与运行手册](#milestone-9完整集成三平台-smoke-与运行手册) | [设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)；[设计 §10.4](m3_transcript_history_implementation_design.md#104-跨操作系统可迁移性) | [API §8.5](m3_transcript_history_api.md#85-更换操作系统或机器的最短流程)；[API §9](m3_transcript_history_api.md#9-排障与验证) |
| [Milestone 10：契约验收与后续交接](#milestone-10契约验收与后续交接) | [设计 §12](m3_transcript_history_implementation_design.md#12-完成标准与接力)；[设计 §1.2](m3_transcript_history_implementation_design.md#12-后续阶段承接) | [契约 §13](../deferred_milestone_contracts.md#13-承接记录)；[前端约定 §5](../frontend_integration_contracts.md#5-m4m9-的前端接力边界) |

以下原任务内容与未完成状态保持不变，链接仅提供定位；修改设计章节标题时须同步校验这些锚点。各 Milestone 的历史完成记录不追溯改写；2026-09-14 审查回补项是当前重新通过该阶段的必要门禁，只有实现和对应测试实际通过后才能打勾。

## 执行原则

- [x] 开始时记录已有用户改动；只修改新 backend、适用 CI、M3 与必要上层文档，不覆盖无关文件。（[设计 §1.1](m3_transcript_history_implementation_design.md#11-本阶段实现)；[设计 §1.2](m3_transcript_history_implementation_design.md#12-后续阶段承接)）
- [x] 不读取或复用归档后端、归档前端与旧契约；以 M1/M2 当前新代码和中心契约为基线。（[契约 §2](../deferred_milestone_contracts.md#2-跨-milestone-硬约束)）
- [x] 直接将旧整体 session 职责迁到 run，真实 Agent session 使用独立上下文；旧 HTTP 路径不保留兼容别名。（[设计 §2](m3_transcript_history_implementation_design.md#2-当前代码与目标模块)；[API §1](m3_transcript_history_api.md#1-基础约定与路由)）
- [x] 坚持四张核心业务表、原正文一份、公共上下文原引用；不新增创建 key 表、关联索引表、状态回放系统或地图专用存储。（[设计 §4](m3_transcript_history_implementation_design.md#4-postgresql-schema)）
- [x] 细节由实现者在已确认保证内完成，不逐字段等待用户审批；实质改变行为/数据保证/范围才重新对齐。（[决策记录 §4](m3_transcript_history_design.md#4-工程定型与后续阶段边界)）
- [x] 每个阶段先完成相关工作与 focused tests；达到阶段出口再继续。最后运行适用完整回归和平台 smoke，不重复堆叠无关测试。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）
- [x] 数据库约束与事务使用真实 PostgreSQL 验证；fakes 负责确定性时序和故障，不代替真实提交证据。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）
- [x] 时间使用 ClockPort，时序测试用 gate/可控计时，不真实等待整轮退避或 60 秒清理。（[设计 §3.3](m3_transcript_history_implementation_design.md#33-时间)；[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）
- [x] 依赖、uv.lock、README、OpenAPI、API/SSE fixture 和 smoke 与功能同批维护。（[API §9](m3_transcript_history_api.md#9-排障与验证)）
- [x] 按 GLOBAL-C11 保持 macOS/Windows/Linux 同一份业务代码、依赖锁、配置格式与 schema；换系统通过配置和重建运行环境完成，不要求手工改源码。（[设计 §10.4](m3_transcript_history_implementation_design.md#104-跨操作系统可迁移性)）
- [x] 文档不出现内部参考项目名称、私有路径或专有实现标识；使用本项目定义及公开技术说明。（[决策记录 §1](m3_transcript_history_design.md#1-依据与当前基线)）
- [x] 每阶段登记实际改动位置、命令、环境和结果；缺少的证据保持待办或 partial。（[证据记录格式](#实际证据记录格式)）

## 成功标准

- [ ] 一个 run 可拥有问题解析与多个 COMID sessions；状态、身份、时间与独立上下文正确，未执行节点不预建 session；workflow 失败或返回时不存在遗留的非终态 session。（[设计 §3.2](m3_transcript_history_implementation_design.md#32-当前状态)）
- [ ] 全新 PostgreSQL 环境能独立初始化/升级，三个平台按文档搭建；启动严格检查并修复遗留后才接收请求。（[设计 §10.2](m3_transcript_history_implementation_design.md#102-配置与命令)；[设计 §10.3](m3_transcript_history_implementation_design.md#103-启动恢复和关闭)）
- [ ] 三系统分别作为迁移目标，用同版代码和锁安装运行；接回同一测试数据库后原身份、历史游标、回执与全文引用保持有效，不转移未保存内存或自动恢复 Agent。（[设计 §10.4](m3_transcript_history_implementation_design.md#104-跨操作系统可迁移性)；[API §8.5](m3_transcript_history_api.md#85-更换操作系统或机器的最短流程)）
- [ ] 创建事务提交后才执行，同 key + 原 body 返回原 201 回执；任意无法证明回滚的提交异常都先核实，响应丢失、并发和未知提交都不会重复执行。（[设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)）
- [ ] 过程增量保存、同批原子、稳定身份及明确提交确认成立；生成中草稿不能被最大序号误判已保存，pending session 的创建核实不能误判终态引用。（[设计 §6.2](m3_transcript_history_implementation_design.md#62-runwriter)；[设计 §6.3](m3_transcript_history_implementation_design.md#63-原子提交超时与核实)）
- [ ] 有限补写、第三次失败后两秒的完成交付窗口、手动恢复和唯一业务终态相互独立；确定性坏引用不能杀死唯一 writer 并留下不可恢复 pending。（[设计 §6.4](m3_transcript_history_implementation_design.md#64-重试和交付窗口)；[设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理)）
- [ ] 当前读取与严格历史共享 DTO/解析/全文实现；PG 故障时 live 可读，history 不混入内存，读取侧安全复核不能破坏合法 JSON。（[设计 §7.1](m3_transcript_history_implementation_design.md#71-一次选择来源)）
- [x] 只有 run 历史列表分页，任一实际 session 的完整过程与显式公共全文都可读取。（[设计 §7.2](m3_transcript_history_implementation_design.md#72-run-详情与-session-完整过程)；[设计 §7.4](m3_transcript_history_implementation_design.md#74-run-历史分页)）
- [ ] 两层 SSE、首次完整过程、整数续传、三阶段消息和明确 stream_end/410 成立；正文不逐帧重复落库，流式正文、最终正文与 metadata 保持一致。（[设计 §8.2](m3_transcript_history_implementation_design.md#82-session-过程)；[设计 §8.3](m3_transcript_history_implementation_design.md#83-续传握手与结束)）
- [ ] 取消即固定原生成中正文，晚到审计仅历史可见；凭据跨片段、取消边界和日志均不泄漏，通用在途结果也不能绕过取消边界。（[设计 §9.1](m3_transcript_history_implementation_design.md#91-原消息与晚到审计)；[设计 §9.2](m3_transcript_history_implementation_design.md#92-凭据边界)）
- [ ] 只有终态、执行收敛、全部实际保存和完整 grace 后清理；workflow 失败和 session 取消同样收束活动消息，重启不自动恢复 Agent 或补造未保存内容。（[设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理)；[设计 §10.3](m3_transcript_history_implementation_design.md#103-启动恢复和关闭)）
- [ ] 中心全部适用契约有实现位置和重新验证证据，API、OpenAPI、前端约定、fixture 与运行时一致，未完成的跨平台验证或业务后续项准确登记。（[设计 §12](m3_transcript_history_implementation_design.md#12-完成标准与接力)；[契约覆盖矩阵](#契约覆盖矩阵)）

## Milestone 0：基线、适用契约与工作区

### 工作

- [x] 记录分支/HEAD、git status、已有改动和 Python/依赖版本；保留 M2 原有文档改动。（[证据记录格式](#实际证据记录格式)）
- [x] 阅读 M1/M2 设计、实现、API 与当前测试，确认整体协调器、ClockPort、PublicError、CorrelationContext、SSE encoder/response、redaction 和 factory/lifespan 入口。（[M1 详细设计](../M1/m1_project_shell_domain_design.md)；[M2 接力与验收](../M2/m2_analysis_session_sse_implementation_todo.md#后续接力入口与验收)）
- [x] 核实现有 TranscriptStorePort 及架构导入约束，列出所有调用方、测试替身与将迁移的旧名称。（[设计 §2](m3_transcript_history_implementation_design.md#2-当前代码与目标模块)）
- [x] 核对中心 GLOBAL、M3-C01–C20、M3-A01–A12 与 M2 接力；记录仍未取得的 M2 三平台证据，不猜测通过。（[契约 §2](../deferred_milestone_contracts.md#2-跨-milestone-硬约束)；[M2 接力与验收](../M2/m2_analysis_session_sse_implementation_todo.md#后续接力入口与验收)）
- [x] 列出目标目录和依赖改动；保持前端实现、真实业务 Agents 与无关 refactor 在范围外。（[设计 §1.2](m3_transcript_history_implementation_design.md#12-后续阶段承接)；[设计 §2](m3_transcript_history_implementation_design.md#2-当前代码与目标模块)）
- [x] 确认参考代码仅作为只读设计依据，交付文档不包含内部名字。（[决策记录 §1](m3_transcript_history_design.md#1-依据与当前基线)）

### Tests / Verify

- [x] 在现有可用环境运行基线：`python -m pytest backend/tests -q`。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）
- [x] `git diff --check`；记录本机基线结果及确实存在的失败。（[证据记录格式](#实际证据记录格式)）
- [x] 对照架构测试确认 ports 不反向依赖 application/infrastructure。（[设计 §2](m3_transcript_history_implementation_design.md#2-当前代码与目标模块)）

### 阶段出口与记录

#### Milestone 0 完成记录（2026-09-13）

开始时位于 `codex/m3-transcript-history`，跟踪 `origin/m3-transcript-history`，工作区和 index 均无改动；从该 HEAD 新建 `codex/m3-milestone-0`。基线提交为 `de6606b25be6af5a5bf6b18b6a155b5a098f9d04`。本阶段仅修改本清单，M1/M2 实现、M2 历史文档、归档目录和前端均未改动，也没有需要合并或覆盖的用户未提交内容。

本机环境：macOS 26.6.2（arm64），uv 0.11.23，锁定环境 Python 3.12.14。`backend/pyproject.toml` 要求 Python >=3.11；本次解析的直接依赖为 FastAPI 0.141.1、Pydantic 2.13.5、pydantic-settings 2.15.0、tzdata 2026.3、Uvicorn 0.52.4，开发依赖为 HTTPX 0.28.1、pytest 9.1.1；Starlette 1.6.0 为本次相关传递依赖。当前 shell 没有独立的 `python` 命令，因此实际通过锁定 uv 环境执行同一 `python -m pytest` 入口，没有修改依赖或 lockfile。

#### 当前实现入口与迁移清单

| 边界 | 当前真实入口 | M3 承接结论 |
|---|---|---|
| 整体协调与状态 | `backend/app/application/analysis_sessions/coordinator.py:SessionCoordinator`、`runtime.py:SessionRuntime`、`workflow.py:AnalysisWorkflow/WorkflowContext` | 当前对象代表整次分析，迁为 run 级 coordinator/runtime/workflow；真实 Agent session 新建独立 runtime 与五态。原短临界区、取消竞争、task ownership 和 quiesced 语义保留。 |
| 时间与公开事实 | `backend/app/ports/clock.py:ClockPort`、`infrastructure/runtime/system_clock.py:SystemClock`、`application/analysis_sessions/models.py:PublicError/CorrelationContext/LifecycleEvent` | 继续以 ClockPort 产生 UTC 时间；PublicError 与关联模型扩展 run/session 身份。M2 lifecycle v1 只保留历史解析，新写使用区分 run/session 的 v2。 |
| HTTP 与 SSE | `api/http/analysis_sessions.py`、`api/schemas/analysis_sessions.py`、`api/sse/encoder.py`、`api/sse/response.py`、`api/http/errors.py`、`api/http/request_context.py` | 旧 `/api/analysis/sessions` 三路由直接替换为 M3 十路由；复用安全错误与 request ID 边界，SSE 扩展为 run snapshot 与 session process 两层且以 `stream_end` 取代旧 204。 |
| 脱敏与日志 | `infrastructure/observability/redaction.py`、`json_logging.py` | 保留 Authorization/Cookie/URL/已知凭据保护；M3-C19 调整当前整段隐藏 query 的行为，使普通 query 可诊断而其中凭据仍被移除，并增加流式跨片段边界。 |
| Composition/lifespan | `backend/app/main.py:create_app`、lifespan 与 `backend/app/__main__.py:main` | 保留 clock/workflow/id factory 注入和单 worker；后续按连接、schema、遗留修复、coordinator 的顺序接入 PostgreSQL，并在关闭时收敛 writer/engine。 |
| 测试替身 | `backend/tests/application/test_session_workflow.py` 的 controlled/gate workflows、各测试的 FixedClock/Clock/id factory，以及 `create_app` 注入 | 继续只替换外部能力与确定性时序；不把 fixture outcome、假水质结果或 test-only switch 引入生产。 |

`TranscriptStorePort` 的完整引用盘点只有三处：`backend/app/ports/transcript_store.py` 定义、`backend/app/ports/__init__.py` 导出、`backend/tests/architecture/test_port_imports.py` 的协议/签名/依赖测试。当前没有 production/application 调用方、adapter 或 fake；旧方法为 `append(session_id, item)` 与 `list_for_session(session_id)`。M3 可直接用 ports 层的有界 storage command/query DTO 和 `create_run/commit_batch/verify_batch/list_runs/read_run/read_session/list_items/load_contents/repair_interrupted` 收敛它，不保留逐条 append 兼容入口。该端口必须继续只依赖 stdlib、Domain/Ports；不得反向导入 application、infrastructure、FastAPI、SQLAlchemy 或 adapter。

需要迁移的旧整体 session 表面包括：`application/analysis_sessions` 包与 `AnalysisSessionSnapshot`、`SessionCoordinator`、`SessionRuntime`、`AnalysisWorkflow`、`WorkflowContext`、`SessionStatus`；API schema/response/error 名；`application.state.session_coordinator`、HTTP request state 和日志关联中的旧整体 `session_id`；`/api/analysis/sessions` 路径；`session_sse_smoke.py`；以及 `backend/tests/api/test_analysis_events.py`、`test_analysis_schemas.py`、`test_analysis_sessions.py`，`backend/tests/application/test_session_models.py`、`test_session_state_machine.py`、`test_session_workflow.py`，相关 architecture/observability/smoke 测试。M3 将旧整体身份迁为 run_id，同时为实际 Agent 保留新的 session_id；operation_id 继续表示单次调用。旧 M2 v1 事件作为历史解析事实保留，不保留旧 HTTP 路径兼容别名。

#### 后续目标目录、依赖与范围

生产目标为 `application/analysis_runs/`、`application/transcripts/`、收敛后的 `ports/transcript_store.py`、`infrastructure/persistence/`、`api/http|schemas/analysis_runs.py`、现有 `api/sse/`、`infrastructure/config/settings.py`、`infrastructure/observability/redaction.py`、`main.py`/lifespan，以及 `migrations/`、`alembic.ini`、`compose.yaml`、`.env.example`、`tests/persistence/` 和 `scripts/run_history_smoke.py`。依赖目标为 SQLAlchemy 2.0 asyncio、psycopg 3 binary（>=3.2,<4）和 Alembic（>=1.13,<2），由 Milestone 2 同批更新 `pyproject.toml` 与 `uv.lock`；Milestone 0 未提前安装或修改它们。

范围外保持为：归档后端/前端和旧契约、当前前端实现、真实 query parser/水体 Agent/LLM/tool/skill/hook 生产方、M4 BFS、M6 综合指标、M7 DAG 策略、M8 跨 run RAG、M9 地图与删除策略，以及无关重构。设计参考只作为只读依据；本阶段未读取或复用归档实现，交付记录不加入内部参考工程名称、私有路径或专有实现标识。

#### 验证与契约结论

从仓库根执行：

```text
uv run --project backend --locked python -m pytest backend/tests -q
602 passed in 11.28s

uv run --project backend --locked python -m pytest backend/tests/architecture -q
55 passed in 0.98s

git diff --check
退出 0
```

架构专项中的 `test_port_modules_have_no_forbidden_dependencies_or_placeholder_payloads` 与 `test_port_package_contains_only_protocols_and_errors` 证明当前 Ports 不反向依赖 application/infrastructure，application/API runtime ownership 门禁也保持通过。完整回归没有已知基线失败。

已逐项核对 `GLOBAL-C01`–`GLOBAL-C11`、`M2-C01`–`M2-C13`、`M3-C01`–`M3-C20` 和 `M3-A01`–`M3-A12`。Milestone 0 只建立这些约束的适用基线和后续实现位置，不将任何 M3 业务 contract 标记为 implemented/partial。M2 文档记录的修复后 Ubuntu/macOS/Windows + Python 3.11 完整 tests 与 health/session SSE 两项 smoke 的实际 CI 结果仍未取得；本次 macOS/arm64/Python 3.12 本机 602 项通过不能替代该三平台证据。真实 PostgreSQL、schema、迁移、路由、持久化及跨系统同库切换证据仍归后续 Milestone，状态保持待办。

## Milestone 1：Run / Agent session 模型和统一状态入口

### 工作

- [x] 将旧整体 AnalysisSession/SessionCoordinator/Workflow 的职责迁为 run，迁移关联 ID 与调用方。（[设计 §2](m3_transcript_history_implementation_design.md#2-当前代码与目标模块)）
- [x] 新增 AgentSessionRuntime 与 SessionSummary，明确 query_parser/null COMID 和 waterbody/有效 COMID。（[设计 §3.2](m3_transcript_history_implementation_design.md#32-当前状态)；[API §3.2](m3_transcript_history_api.md#32-runsummary-与-sessionsummary)）
- [x] run 六态、session 五态、不可变终态及取消优先竞争沿用统一状态机，禁止第二套状态推演逻辑。（[设计 §3.2](m3_transcript_history_implementation_design.md#32-当前状态)）
- [x] 实现准备执行时创建 session；parser 每 run 一次、水体每 run/COMID 一次，不为等待/排除节点创建上下文。（[设计 §3.2](m3_transcript_history_implementation_design.md#32-当前状态)；[设计 §4.2](m3_transcript_history_implementation_design.md#42-analysis_sessions)）
- [x] 原请求/创建/BFS 等归 run，Agent 消息归具体 session；operation_id 对一次调用稳定。（[设计 §3.1](m3_transcript_history_implementation_design.md#31-身份与顺序)；[设计 §4.3](m3_transcript_history_implementation_design.md#43-transcript_items)）
- [x] 统一分配原 item_id、run transcript_sequence 和 created_at；生成开始时可先分配草稿身份。（[设计 §3.1](m3_transcript_history_implementation_design.md#31-身份与顺序)；[设计 §3.3](m3_transcript_history_implementation_design.md#33-时间)）
- [x] 建立短临界区内的事实/状态 mutation 入口，DB/网络等待留在锁外；running 无公开事件也有必需状态变更。（[设计 §3.2](m3_transcript_history_implementation_design.md#32-当前状态)；[设计 §6.2](m3_transcript_history_implementation_design.md#62-runwriter)）
- [x] 明确 mutable created_at/updated_at 与 immutable created_at，API occurred_at 同源；真实业务时间保留。（[设计 §3.3](m3_transcript_history_implementation_design.md#33-时间)）

### Tests

- [x] 两层状态转换、准备失败、取消、终态不可改及已完成子 session 保持完成。（[设计 §3.2](m3_transcript_history_implementation_design.md#32-当前状态)）
- [x] 一个 run 多 session 的上下文隔离、所有上游 gate 后只创建/执行一次。（[设计 §3.2](m3_transcript_history_implementation_design.md#32-当前状态)）
- [x] run-root/session 归属、UUID/COMID、UTC、并发序号与过滤后的缺口。（[设计 §3.1](m3_transcript_history_implementation_design.md#31-身份与顺序)；[设计 §4.2](m3_transcript_history_implementation_design.md#42-analysis_sessions)）
- [x] 读取/重试/确认不刷新时间；生成完成不重新分配 ID/序号/时间。（[设计 §3.1](m3_transcript_history_implementation_design.md#31-身份与顺序)；[设计 §3.3](m3_transcript_history_implementation_design.md#33-时间)）
- [x] 新 factory 与 fakes 的导入/依赖方向检查。（[设计 §2](m3_transcript_history_implementation_design.md#2-当前代码与目标模块)）

### Verify

- [x] `python -m pytest backend/tests/domain backend/tests/application backend/tests/architecture -q`。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）

阶段出口：模型和统一状态入口可独立测试，M1 领域不变；本阶段不宣称具备数据库持久化。

#### Milestone 1 完成记录（2026-09-13）

本阶段在 `codex/m3-milestone-0` 上完成。旧整体 analysis session 的应用包、HTTP schema/路由、运行时、测试、smoke、CI 命令和当前 README 已整体迁为 run 命名，旧 `/api/analysis/sessions` 未保留兼容别名；M1/M2 历史文档保持原事实，不回写为 M3 已完成协议。`AnalysisRunSnapshot`/`RunCoordinator`/`RunRuntime` 保留原整体 workflow ownership，并新增独立的 `SessionSummary` 与 `AgentSessionRuntime`。

统一状态入口覆盖 run 六态、Agent session 五态、pending 准备失败、终态不可改、取消优先和 run 取消时保留已完成/失败子 session。Agent session 只在准备执行边界创建；query parser 按 run、水体按 run + COMID 去重，上游 gate 测试确认 gate 全部打开前不创建，打开后并发调用只创建一次且只领取一次执行。原请求、run lifecycle 与 BFS 使用 run scope，Agent lifecycle/消息使用 session scope；统一入口在短 live 临界区内分配稳定 `item_id`、run 级 `transcript_sequence`、`created_at`、operation/session 身份和状态 mutation，日志在解锁后执行，未引入 DB 或网络等待。

时间继续只由 `ClockPort` 生成并规范为 UTC。run/session 可变快照只在真实状态变化时更新 `updated_at`；item 身份只有原 `created_at`，生命周期 `occurred_at` 与原变化同源。读取、过滤、重复准备、确认式读取和终态拒绝均不重新取时间或重分配身份；session 筛选保留 run 序号缺口。`record_id_factory` 已从 `create_app` 传入 coordinator，测试 fake 与应用/API/Ports 的依赖门禁保持通过。

本机锁定 uv 环境（macOS arm64、Python 3.12.14）实际执行：

- `python -m pytest backend/tests/domain backend/tests/application backend/tests/architecture -q`：374 passed。
- `python -m pytest backend/tests -q`：625 passed。
- `git diff --check`：通过。

阶段出口已满足：模型和统一状态入口可独立测试，`backend/app/domain` 未改动。本阶段没有新增数据库依赖、schema、adapter 或持久化成功声明；这些内容继续由 Milestone 2 及后续阶段承接。

## Milestone 2：PostgreSQL、四表 schema 与可复现环境

### 工作

- [x] 新增 SQLAlchemy Core asyncio、psycopg binary、Alembic 依赖和锁文件；版本范围与实际解析版本记录一致。（[设计 §10.1](m3_transcript_history_implementation_design.md#101-技术选择)）
- [x] 创建 analysis_runs、analysis_sessions、transcript_items、transcript_contents 及 Alembic 单链迁移。（[设计 §4](m3_transcript_history_implementation_design.md#4-postgresql-schema)；[设计 §10.2](m3_transcript_history_implementation_design.md#102-配置与命令)）
- [x] 实现 PK/FK、同 run 联合归属、run 序号唯一、可空 UUID key 唯一、角色/COMID、终态引用和行正文互斥约束。（[设计 §4](m3_transcript_history_implementation_design.md#4-postgresql-schema)）
- [x] created/terminal/request 引用使用延迟外键支持原子创建；不添加完整配置、结果、副本正文或独立 key 表。（[设计 §4.1](m3_transcript_history_implementation_design.md#41-analysis_runs)；[设计 §4.2](m3_transcript_history_implementation_design.md#42-analysis_sessions)）
- [x] 写清跨表全文存在/互斥的 adapter 事务校验与行 CHECK 的区别；非法输入不能通过正常 store 写入口。（[设计 §4.4](m3_transcript_history_implementation_design.md#44-transcript_contents)）
- [x] 增加实际历史列表/目录/完整过程所需索引，不预建全文、confidence 或关联查询索引。（[设计 §4.5](m3_transcript_history_implementation_design.md#45-索引引用及事务边界)）
- [x] 新增 backend/compose.yaml、.env.example、忽略真实 .env、命名持久卷和 loopback 端口/healthcheck。（[API §8.2](m3_transcript_history_api.md#82-本地-compose)）
- [x] AppSettings 按明确优先级加载 backend/.env；SecretStr 密码不 trim、不回显，分列 URL 构造覆盖特殊字符。（[设计 §10.2](m3_transcript_history_implementation_design.md#102-配置与命令)；[API §8.1](m3_transcript_history_api.md#81-环境变量)）
- [x] 实现 wateragents-db upgrade/check；与 app 共用配置，迁移资源从已安装包/明确项目路径可定位。（[设计 §10.2](m3_transcript_history_implementation_design.md#102-配置与命令)）
- [x] schema 版本只来自 Alembic 单 head；check 不 DDL，未知/缺失/旧/新/多 head 明确失败。（[设计 §10.2](m3_transcript_history_implementation_design.md#102-配置与命令)；[设计 §10.3](m3_transcript_history_implementation_design.md#103-启动恢复和关闭)）
- [x] Windows 正式 CLI、测试和 smoke 复用 Selector loop 初始化；不在已经运行的 loop 中切换。（[设计 §10.1](m3_transcript_history_implementation_design.md#101-技术选择)）
- [x] 更新 README 的本地 Compose、已有 PG、remote TLS、升级/停止/保留卷与单实例限制。（[API §8](m3_transcript_history_api.md#8-跨平台搭建与配置)）
- [x] 将路径/包资源、UTF-8、进程启动与系统差异集中在基础设施边界；初始化和 smoke 不依赖 Bash/GNU/Unix 专用能力，不硬编码开发机路径或依赖当前 cwd。（[设计 §10.4](m3_transcript_history_implementation_design.md#104-跨操作系统可迁移性)）

### Tests

- [x] 真实空测试库 upgrade→check→再次 upgrade；确认 revision 与四表约束。（[设计 §4](m3_transcript_history_implementation_design.md#4-postgresql-schema)；[API §8.3](m3_transcript_history_api.md#83-已有数据库与升级)）
- [x] 重复 key、run 序号、parser/waterbody 身份、跨 run FK 和非法状态的失败。（[设计 §4](m3_transcript_history_implementation_design.md#4-postgresql-schema)）
- [x] inline/external 批次错误、丢全文、重复全文、字节数/时间不一致被 adapter 拒绝并整批回滚。（[设计 §4.4](m3_transcript_history_implementation_design.md#44-transcript_contents)；[设计 §6.3](m3_transcript_history_implementation_design.md#63-原子提交超时与核实)）
- [x] 配置 cwd 独立、环境覆盖、含 @:/#%$ 和空白的密码、sslrootcert、缺密码安全错误。（[API §8.1](m3_transcript_history_api.md#81-环境变量)）
- [x] import/fake 注入不依赖真实数据库；正式启动缺配置明确失败。（[设计 §2](m3_transcript_history_implementation_design.md#2-当前代码与目标模块)；[API §8.1](m3_transcript_history_api.md#81-环境变量)）
- [ ] 三平台目标入口连接真实 PG；不要把仅执行配置解析当成连接验证。（[设计 §10.1](m3_transcript_history_implementation_design.md#101-技术选择)；[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）
- [ ] 相同锁文件在目标平台重新安装；含空格/中文路径、不同 cwd、LF/CRLF 配置和平台对应的原生依赖均可正常使用，不复制旧 venv。（[设计 §10.4](m3_transcript_history_implementation_design.md#104-跨操作系统可迁移性)；[API §8.5](m3_transcript_history_api.md#85-更换操作系统或机器的最短流程)）

### Verify

- [x] `wateragents-db upgrade`、`wateragents-db check`（独立测试环境）。（[API §8.2](m3_transcript_history_api.md#82-本地-compose)；[API §8.3](m3_transcript_history_api.md#83-已有数据库与升级)）
- [x] `python -m pytest backend/tests/persistence backend/tests/architecture -q`。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）
- [x] 记录 PG minor/digest、driver/SQLAlchemy/Alembic 版本、migration revision。（[设计 §10.1](m3_transcript_history_implementation_design.md#101-技术选择)；[证据记录格式](#实际证据记录格式)）

### 审查回补（2026-09-14）

- [x] 测试数据库安全闸门必须读取与正式应用相同的 `.env`/环境配置并在任何 DDL 前比较实际业务库与测试库目标；不能因 `_env_file=None` 看见默认库名而放行真实业务库。（[设计 §10.2](m3_transcript_history_implementation_design.md#102-配置与命令)；[API §9](m3_transcript_history_api.md#9-排障与验证)）
- [x] 默认配置测试显式禁用 `.env`，数据库安全测试覆盖“业务库名只在 `.env`、测试库名与其相同”必定拒绝，以及不同专用测试库正常通过。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）

阶段出口：可复现空库 schema、可靠连接及命令，尚不把启动修复或 transcript 写入标记完成。

#### Milestone 2 完成记录（2026-09-13）

本阶段在 `codex/m3-milestone`、基线 `388a48dc0074dc85ca5a87a83277788a74b8fc26` 上完成；开始时工作区无未提交用户改动。新增 `infrastructure/persistence` 的 SQLAlchemy Core 四表 metadata、单个 Alembic revision `20260913_01`、共享数据库配置/engine、只读 schema check、显式 upgrade CLI、跨表批次记录与原子 store。数据库 CHECK/PK/FK/唯一约束负责行内及关系事实；普通 CHECK 无法验证另一表是否存在全文，因此 store 在进入事务前重新验证 inline XOR contents、全文同批、UTF-8 字节数及原 `created_at`，数据库仍负责事务内全部回滚。没有增加完整配置/结果列、第二份正文、独立 key 表、全文/confidence/关联查询索引，也没有把 live workflow 接入或启动修复提前到本阶段。

配置新增固定 `backend/.env`、不 trim 的 `SecretStr` 密码、分列 `URL.create` 和 TLS 参数；正式 app、数据库 CLI、测试及 smoke 共享 Windows Selector-loop 初始化。`backend/compose.yaml` 使用 `postgres:17`、loopback 端口、`pg_isready` 和命名卷；README 记录本地 Compose、已有/远程 PostgreSQL、TLS、升级/check、停止保留卷、换机重建环境及当前单实例限制。CI 改为三平台安装同一 `uv.lock`，并用 `ikalnytskyi/action-setup-postgres@v8` 启动真实 PostgreSQL 17 后运行包含实库用例的完整测试。

本机环境为 macOS 26.6.2 arm64、uv 0.11.23、Python 3.12.14。锁定直接数据库依赖为 Alembic 1.20.0、SQLAlchemy 2.0.52、psycopg/psycopg-binary 3.3.5；实际测试服务为 PostgreSQL 17.10 Homebrew，非容器，因此本机 image digest 不适用；Compose 镜像保持设计指定的 `postgres:17`，其实际 minor/digest 待每次容器/CI 证据记录。数据库当前 revision 为 `20260913_01`，public 中为四张业务表加 `alembic_version`。

实际验证：

- 全新隔离库依次执行 `wateragents-db upgrade`、`check`、再次 `upgrade`，均到达 `20260913_01`；另一个空库的 `check` 明确失败，查询系统目录确认失败后 public 表数仍为 0。
- `uv run --project backend --locked python -m pytest backend/tests/persistence backend/tests/architecture -q`：89 passed；其中真实 PostgreSQL 用例验证 migration/metadata 无漂移、全部约束、重复 key/序号/parser/waterbody、跨 run 归属、非法状态、external 正文和事务回滚。
- `uv run --project backend --locked python -m pytest backend/tests -q`：659 passed；health smoke 输出 `{"status":"healthy"}`，run/SSE smoke 输出 `{"completed":"passed","cancelled":"passed"}`。
- 从 `/private/tmp/WaterAgents M2 中文` 运行已安装 `wateragents-db check` 成功；单元测试覆盖含空格/中文路径、不同 cwd、LF/CRLF `.env`、`@:/#%$` 及首尾空白密码。`git diff --check` 在提交前复核。

阶段工作和本机适用验证已完成。三平台 CI 入口已经落地，但当前没有本次远端 workflow 的实际链接/结果，所以“三平台真实 PG”和“各目标平台重新安装同锁”两项保持未勾选（partial），不能由 macOS 本机通过替代。Docker 在本机不可用，所以没有伪造 `postgres:17` image digest。Milestone 3 承接 typed content、严格 registry、单份正文编码/预览和引用验证；Milestone 4 以后承接实际创建/增量持久化，Milestone 8 承接启动 schema 检查、遗留修复与关闭收敛。

#### Milestone 2 审查回补完成记录（2026-09-14）

- 实现：`scripts/test_database.py` 通过无参数 `AppSettings()` 读取与正式应用相同的固定 `.env` 和进程环境来源，再用显式 `WATERAGENTS_TEST_DB_NAME` 替换数据库名；默认配置测试显式传入 `_env_file=None`，不受开发者本地配置污染。
- 安全测试：临时 `.env` 只声明业务库名时，相同测试库名在任何 DDL 前被拒绝；使用不同测试库名时保留 env file 中的 host、port、user 和 password，仅替换 db_name。旧的 `_env_file=None` 调用会因测试替身拒绝额外参数而失败，防止该缺口回归。
- 验证：focused 配置/安全/架构测试为 38 passed；隔离 PostgreSQL 17 下 persistence + architecture 为 103 passed；同一测试库下完整 `backend/tests` 为 737 passed。测试使用 `wateragents_business_guard` 作为业务库保护名、`wateragents_m2_review` 作为专用测试库，临时实例及数据目录已停止并删除。
- 限制：本次只完成 2026-09-14 审查回补；Milestone 2 原有 Ubuntu/macOS/Windows 实际 PostgreSQL 和同锁安装证据仍保持未勾选/partial。

## Milestone 3：Typed content、单份正文、引用与安全入口

### 工作

- [x] 完成 immutable transcript、storage batch/read model、严格 registry 和统一 UTF-8 编码。（[设计 §2](m3_transcript_history_implementation_design.md#2-当前代码与目标模块)；[设计 §5.1](m3_transcript_history_implementation_design.md#51-注册器)）
- [x] 生产注册 analysis_request v1、lifecycle_event v2、error v1、runtime_fact v1；runtime_fact 使用明确 fact 联合。（[设计 §5.1](m3_transcript_history_implementation_design.md#51-注册器)）
- [x] 生命周期命名区分 run/session，配置只在 run_created，保留 v1 历史解析与原版本/序号/时间。（[设计 §5.1](m3_transcript_history_implementation_design.md#51-注册器)）
- [x] 已附于原事件的配置或错误不再复制同义 item；tags/RAG 单源，confidence/provenance 按正文适用层级提取。（[设计 §5.1](m3_transcript_history_implementation_design.md#51-注册器)；[设计 §5.2](m3_transcript_history_implementation_design.md#52-消息与元信息)）
- [x] 为所有已知类型实现唯一引用提取器，验证同 run/跨 session、目标存在或同批；保留 AnalysisDetail/AnalysisStep 原引用。（[设计 §4.5](m3_transcript_history_implementation_design.md#45-索引引用及事务边界)）
- [x] 完成 64 KiB 默认阈值、首尾总 2 KiB Unicode 安全预览和 run/item 全文定位。（[设计 §5.3](m3_transcript_history_implementation_design.md#53-单份正文与大内容)；[API §4.4](m3_transcript_history_api.md#44-全文)）
- [x] 新写严格验证；未知历史版本/已知损坏分别处理，不静默删记录或伪装正常业务结果。（[设计 §5.4](m3_transcript_history_implementation_design.md#54-未知版本与损坏)）
- [x] 脱敏先于可读 live/SSE，再由持久化出口复核；日志保留普通 query。（[设计 §9.2](m3_transcript_history_implementation_design.md#92-凭据边界)）
- [x] 完成流式凭据过滤器的私有缓冲、遮蔽状态及安全收尾，不把原始 secret 保留成另一个可读副本。（[设计 §9.2](m3_transcript_history_implementation_design.md#92-凭据边界)）
- [x] 测试专用 fixture_message 注入通用消息适配器，不将假 LLM/水体结果注册进生产。（[设计 §5.1](m3_transcript_history_implementation_design.md#51-注册器)；[设计 §5.2](m3_transcript_history_implementation_design.md#52-消息与元信息)）

### Tests

- [x] typed round-trip、未知/非法版本、错误归类、正常邻近 item 保留。（[设计 §5.1](m3_transcript_history_implementation_design.md#51-注册器)；[设计 §5.4](m3_transcript_history_implementation_design.md#54-未知版本与损坏)）
- [x] UTF-8 65535/65536/65537 字节、预览 head/tail 各上限、自定义阈值、多字节字符。（[设计 §5.3](m3_transcript_history_implementation_design.md#53-单份正文与大内容)）
- [x] 同文独立事实不同 ID，公共内容只存原 item，refs 跨 session 合法/跨 run 拒绝。（[设计 §3.1](m3_transcript_history_implementation_design.md#31-身份与顺序)；[设计 §4.5](m3_transcript_history_implementation_design.md#45-索引引用及事务边界)）
- [x] 生成中引用依赖等待，已保存/同批目标合法，非法目标整批失败。（[设计 §4.5](m3_transcript_history_implementation_design.md#45-索引引用及事务边界)）
- [x] 所有合成凭据切分点、普通 query、异常 repr/JSON 转义、URL/Authorization/Cookie、取消边界与最终正文。（[设计 §9.1](m3_transcript_history_implementation_design.md#91-原消息与晚到审计)；[设计 §9.2](m3_transcript_history_implementation_design.md#92-凭据边界)）
- [x] 正常文本持续增量，不能等全文结束才实现表面“安全流式”。（[设计 §9.2](m3_transcript_history_implementation_design.md#92-凭据边界)）

### Verify

- [x] `python -m pytest backend/tests/application backend/tests/observability backend/tests/persistence -q`。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）

### 审查回补（2026-09-14）

- [x] 收紧静态脱敏语义：Authorization/header、敏感 key/assignment 和显式 private value 继续遮蔽，普通 `Basic water quality`、`Bearer population estimate` 等业务文本保持原样。（[设计 §9.2](m3_transcript_history_implementation_design.md#92-凭据边界)）
- [x] typed content 在 finalize 边界校验引用属于同一 run 且目标已经 reserve；确定不存在或跨 run 的引用同步拒绝，不能先进入 buffer 再由后台 writer 发现。（[设计 §4.5](m3_transcript_history_implementation_design.md#45-索引引用及事务边界)）
- [x] 增加普通 Basic/Bearer 文本、真实 Authorization/header/assignment、显式 private value 和非法/等待引用的边界测试，证明安全与业务正文均不被破坏。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）

阶段出口：一份安全原正文贯穿 live、存储和读取模型；registry 为后续生产方提供明确接入点。

实际验证：

- `backend/.venv/bin/python -m pytest backend/tests/application backend/tests/observability backend/tests/persistence -q`：283 passed，5 skipped；跳过项为需要外部 PostgreSQL 的既有集成测试。
- `backend/.venv/bin/python -m pytest backend/tests -q`：691 passed，5 skipped；真实 socket smoke 在允许本机回环端口的环境执行并通过。
- 审查回补聚焦验证：`backend/.venv/bin/python -m pytest backend/tests/application/test_transcript_buffer.py backend/tests/application/test_run_writer.py backend/tests/observability/test_json_logging.py -q`：88 passed。
- 审查回补指定验证：在隔离 PostgreSQL 下运行 `backend/.venv/bin/python -m pytest backend/tests/application backend/tests/observability backend/tests/persistence -q`：340 passed，无 skip。
- 审查回补完整回归：在同一隔离 PostgreSQL 下运行 `backend/.venv/bin/python -m pytest backend/tests -q`：745 passed，无 skip；`python -m compileall -q backend/app backend/tests` 与 `git diff --check` 通过。

## Milestone 4：创建原子事务、幂等和所有权收敛

### 工作

- [x] HTTP body 只读一次，SHA-256 在业务解析/默认值/trim 前计算；不保存未脱敏 raw bytes。（[设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)；[API §2](m3_transcript_history_api.md#2-创建与幂等)）
- [x] 首次事务包含 run、原请求、run_created 配置、key/fingerprint/小型原回执及引用，确认后才允许执行。（[设计 §4.1](m3_transcript_history_implementation_design.md#41-analysis_runs)；[设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)）
- [x] 同 key 先读原指纹/回执；不因当前默认配置或请求 DTO 的后来变化改写已接受响应。（[设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)）
- [x] 用 DB UNIQUE 裁决竞争；内存 owner 只负责当前创建任务/注册窗口，不作为幂等权威。（[设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)）
- [x] 等待者共享默认 5 秒 deadline，确认回滚才接手，超时不取消 owner 或伪称回滚。（[设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)）
- [x] HTTP 断开后 owner 仍完成实际提交核实/注册；读取与 SSE 不把提交后注册前窗口误判 410。（[设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)；[设计 §8.3](m3_transcript_history_implementation_design.md#83-续传握手与结束)）
- [x] 未知提交保留原身份，通过原事务收尾和同 run 事务级 advisory lock 后的新查询核实结果；未确认不执行、不重建 run，幂等竞争仍受 DB UNIQUE 约束。（[设计 §6.3](m3_transcript_history_implementation_design.md#63-原子提交超时与核实)）
- [x] task 启动失败保持原 201，追加一次 workflow_start_failed 终态；取消若先胜出则保持取消。（[设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)）
- [x] 原回执与 key 无自动过期；当前保存状态/HTTP request_id 不进入不可变 receipt。（[设计 §4.1](m3_transcript_history_implementation_design.md#41-analysis_runs)；[API §2](m3_transcript_history_api.md#2-创建与幂等)）

### Tests

- [x] 同 key 同 body 串行/并发均同 run、201、同回执与一次执行。（[设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)；[API §2](m3_transcript_history_api.md#2-创建与幂等)）
- [x] JSON 空白、顺序、转义、值及字段有无改变返回 409；非法 key 422。（[API §2](m3_transcript_history_api.md#2-创建与幂等)）
- [x] 不同 key 相同 body、不带 key 的独立提交各建新 run。（[API §2](m3_transcript_history_api.md#2-创建与幂等)）
- [x] 终态、默认设置变化、重启和 live 清理后原 receipt 保持，HTTP request_id 每次不同。（[设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)；[API §2](m3_transcript_history_api.md#2-创建与幂等)）
- [x] 创建回滚无执行；提交响应丢失可核实；SELECT 暂时无行不能误认回滚。（[设计 §6.3](m3_transcript_history_implementation_design.md#63-原子提交超时与核实)）
- [x] 等待超时、客户端断开、注册窗口和启动失败/取消竞争。（[设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)）
- [x] 缺失 key 的网络重发不被文档误称为可去重。（[API §2](m3_transcript_history_api.md#2-创建与幂等)）

### Verify

- [x] `python -m pytest backend/tests/api backend/tests/application backend/tests/persistence -q`。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）

### 审查回补（2026-09-14）

- [x] `create_run` 除明确幂等冲突和任务取消外，所有无法证明事务回滚的连接/驱动异常均进入原候选核实；已提交创建必须注册 live runtime 并且只启动一次 workflow。（[设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)；[设计 §6.3](m3_transcript_history_implementation_design.md#63-原子提交超时与核实)）
- [x] `verify_creation` 按 expected item identity 重排或映射外置正文，不依赖随机 UUID 的 SQL 排序与 transcript 顺序偶然一致。（[设计 §6.3](m3_transcript_history_implementation_design.md#63-原子提交超时与核实)）
- [x] 故障注入覆盖“提交成功后抛 `OSError`/驱动连接异常”、确认回滚和核实暂时不可用；真实 PostgreSQL 覆盖小 inline 阈值及逆序 UUID 的创建核实。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）

阶段出口：创建路由的持久化保证成立，测试证明数据库唯一性和一次执行，不能仅依赖内存 map。

#### Milestone 4 完成记录（2026-09-13）

- 代码状态：`codex/m3-milestone` 分支，本地提交 `0d9e9d4`；保留此前 Milestone 0–3 提交，未改动归档代码或无关目录。
- 实现位置：创建候选/不可变回执与错误位于 `application/analysis_runs/creation.py`；协调、未知提交核实、注册窗口和 workflow gate 位于 `application/analysis_runs/coordinator.py`；PostgreSQL 原子创建、事务级 advisory lock 与真实唯一约束裁决位于 `infrastructure/persistence/store.py`；HTTP 单次读 body、UUID v4、错误映射和生产 store 装配位于 `api/http/analysis_runs.py`、`api/http/errors.py` 与 `main.py`。`infrastructure/persistence/memory_store.py` 仅作为确定性测试 fake，并非生产幂等权威。
- 环境：Darwin 25.6.0 arm64、Python 3.12.14、PostgreSQL 17.10、SQLAlchemy 2.0.52、psycopg 3.3.5、Alembic 1.20.0；schema revision `20260913_01`。
- 指定验证：`WATERAGENTS_TEST_POSTGRES_PORT=55433 ... backend/.venv/bin/python -m pytest backend/tests/api backend/tests/application backend/tests/persistence -q`：417 passed，真实 PostgreSQL 测试未跳过。
- 完整回归：`WATERAGENTS_TEST_POSTGRES_PORT=55433 ... backend/.venv/bin/python -m pytest backend/tests -q`：721 passed。
- 契约结论：Milestone 4 的创建事务、原 bytes 幂等、DB UNIQUE 竞争、未知提交收敛、断连/超时所有权和一次执行在本机真实 PostgreSQL 上满足；无 key 请求的网络重发明确不保证去重。增量 writer、统一历史读取、两层 SSE、启动遗留修复和三平台远端证据仍分别归 Milestone 5–10。
- 审查回补故障注入：`backend/.venv/bin/python -m pytest backend/tests/application/test_run_creation.py -q`：14 passed；覆盖提交后 timeout、`OSError`、SQLAlchemy 驱动异常，确认回滚及核实暂时不可用。
- 审查回补 PostgreSQL 验证：`backend/.venv/bin/python -m pytest backend/tests/persistence/test_postgres_persistence.py -q`：12 passed；小 inline 阈值与逆序 UUID 用例强制两份创建正文外置并完成未知提交核实。
- 审查回补指定验证：在同一隔离 PostgreSQL 下运行 `backend/.venv/bin/python -m pytest backend/tests/api backend/tests/application backend/tests/persistence -q`：439 passed，无 skip。
- 审查回补完整回归：在同一隔离 PostgreSQL 下运行 `backend/.venv/bin/python -m pytest backend/tests -q`：748 passed，无 skip；`python -m compileall -q backend/app backend/tests` 与 `git diff --check` 通过。

## Milestone 5：串行写入、确认集合与有限补写

### 工作

- [x] 每 run 只建一个 writer；待写引用原 buffer，准备/冻结批次在锁内，SQL 在锁外。（[设计 §6.2](m3_transcript_history_implementation_design.md#62-runwriter)）
- [x] 100 items 目标、空闲立即写、状态单独触发、原子组不可拆、无空 SQL。（[设计 §6.2](m3_transcript_history_implementation_design.md#62-runwriter)）
- [x] 草稿占据原序号但不提前写库；无依赖可写记录可先保存，按实际 item/mutation 维护确认。（[设计 §3.1](m3_transcript_history_implementation_design.md#31-身份与顺序)；[设计 §6.2](m3_transcript_history_implementation_design.md#62-runwriter)）
- [x] 运行中失败仍保留 live/分析，重试冻结原批次，不覆写已保存原事实、不重排身份/时间。（[设计 §6.2](m3_transcript_history_implementation_design.md#62-runwriter)；[设计 §6.4](m3_transcript_history_implementation_design.md#64-重试和交付窗口)）
- [x] 默认 5 秒覆盖 acquire/SQL/commit，旧调用退出与连接失效收尾后才退避。（[设计 §6.3](m3_transcript_history_implementation_design.md#63-原子提交超时与核实)）
- [x] 实现提交后响应丢失的核实，检查同批身份/正文/适用状态；不能把未知结果当成功或回滚。（[设计 §6.3](m3_transcript_history_implementation_design.md#63-原子提交超时与核实)）
- [x] 首次 + 最多 5 次重试、1/2/4/8/16 秒；adapter 无隐藏重试，新事实不重置预算。（[设计 §6.4](m3_transcript_history_implementation_design.md#64-重试和交付窗口)）
- [x] 成功分析冻结此前必需事实集合；第三次重试失败后两秒到期交付警告终态，已过期不重新等待。（[设计 §6.4](m3_transcript_history_implementation_design.md#64-重试和交付窗口)）
- [x] 终态及收尾另存原子组，保存失败不重选终态；唯一 run_completed 可表达 persistence error。（[设计 §6.4](m3_transcript_history_implementation_design.md#64-重试和交付窗口)）
- [x] 派生 PersistenceInfo，草稿 pending、实际 warning、耗尽优先；恢复后清当前 warning、保留原事件 warning。（[设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理)；[API §3.1](m3_transcript_history_api.md#31-来源与保存情况)）
- [x] 实现手动恢复服务：exhausted 新一轮，pending 复用，up_to_date 无操作；HTTP 在 Milestone 6 接入。（[设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理)；[API §6.2](m3_transcript_history_api.md#62-persistenceretry)）

### Tests

- [x] 两 run 并行、同 run 串行；状态/事件/全文/refs 同批失败全部回滚。（[设计 §4.5](m3_transcript_history_implementation_design.md#45-索引引用及事务边界)；[设计 §6.3](m3_transcript_history_implementation_design.md#63-原子提交超时与核实)）
- [x] 较大序号已保存而小草稿未定稿时仍 pending；所有 group/mutation 确认后才 up_to_date。（[设计 §6.2](m3_transcript_history_implementation_design.md#62-runwriter)；[设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理)）
- [x] 冻结失败批次优先、新记录不抢跑、不额外 writer、无空事务。（[设计 §6.2](m3_transcript_history_implementation_design.md#62-runwriter)）
- [x] 每次超时共享预算、取消收尾、unknown commit 核实及不并行重写。（[设计 §6.3](m3_transcript_history_implementation_design.md#63-原子提交超时与核实)）
- [x] 各次退避、第三次失败后两秒、窗口已过、窗口中恢复/取消、后续终态写入失败。（[设计 §6.4](m3_transcript_history_implementation_design.md#64-重试和交付窗口)）
- [x] 耗尽后 DB 恢复/GET/新内容不唤醒；手动重复/并发不重置执行中预算。（[设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理)）
- [x] 保存恢复事实只追加一次，不因保存自己递归追加。（[设计 §5.1](m3_transcript_history_implementation_design.md#51-注册器)）

### Verify

- [x] `python -m pytest backend/tests/application backend/tests/persistence -q`。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）

### 审查回补（2026-09-14）

- [ ] 写入与 `verify_batch` 共用 session `terminal_item_id` 的期望计算；pending/running session 的创建/状态 item 不能被误作终态引用。（[设计 §4.2](m3_transcript_history_implementation_design.md#42-analysis_sessions)；[设计 §6.3](m3_transcript_history_implementation_design.md#63-原子提交超时与核实)）
- [ ] writer 对确定性非法批次不得静默退出：正常入口提前拒绝坏引用，任务最外层仍将意外非取消异常转为明确的不可重试 warning/诊断，不能留下无人负责的普通 pending。（[设计 §6.2](m3_transcript_history_implementation_design.md#62-runwriter)；[设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理)）
- [ ] 持久化失败日志携带与公开 warning 相同的 `error_id` 及实际 attempt，不借用业务 `last_error`，使客户端错误可直接关联日志。（[API §7](m3_transcript_history_api.md#7-错误与排查字段)）
- [ ] memory store 与真实 PostgreSQL 运行同一 unknown-commit 核实契约；增加 pending session creation-only、坏引用 writer 存活和 warning/log ID 一致性测试。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）

阶段出口：持久化与业务状态分别正确，故障矩阵证明真实确认和原子性，而非仅测试入队成功。

#### Milestone 5 完成记录（2026-09-13）

- 代码状态：`codex/m3-milestone` 分支，本地提交 `1ba321a`；保留此前 Milestone 0–4 本地提交，未改动归档代码或无关目录。
- 实现位置：`application/analysis_runs/persistence.py` 定义不可变 group/batch、确认结果与 `PersistenceInfo`；`coordinator.py` 和 `runtime.py` 实现每 run 单 writer、确认集合、冻结批次、有限退避、交付窗口、恢复事实和手动恢复服务；`infrastructure/persistence/store.py` 实现共享 deadline、连接失效收尾、advisory lock、增量原子提交与未知提交核实。HTTP retry 留给 Milestone 6。
- 性能边界：长 Unicode 正文的敏感信息扫描增加关键词门控；90KB 测试正文由约 50 秒降至亚毫秒级脱敏，并在真实 PostgreSQL 中验证外置正文完整写入。
- 指定验证：`WATERAGENTS_TEST_POSTGRES_PORT=55458 ... backend/.venv/bin/python -m pytest backend/tests/application backend/tests/persistence -q`：260 passed，真实 PostgreSQL 测试未跳过。
- 完整回归：`WATERAGENTS_TEST_POSTGRES_PORT=55458 ... backend/.venv/bin/python -m pytest backend/tests -q`：746 passed。
- 契约结论：单 run 串行/多 run 并行、草稿缺口、100 items 目标、原子组、确认集合、共享超时预算、未知提交核实、首次加五次补写、交付窗口、终态不重选、派生保存状态和手动恢复均满足；真实 PostgreSQL 覆盖连续连接复用、状态/事件/全文提交及冲突整批回滚。

## Milestone 6：统一读取、历史分页与 HTTP 契约

### 工作

- [x] 共享 DTO/registry/完整列表/全文装载，固定 auto/history 来源，不维护第二套模型。（[设计 §7.1](m3_transcript_history_implementation_design.md#71-一次选择来源)；[API §3](m3_transcript_history_api.md#3-当前与历史的共同返回模型)）
- [x] live 短临界区取快照；PG 多语句读取使用一致的只读事务，不混入 live 配置/状态/引用。（[设计 §7.1](m3_transcript_history_implementation_design.md#71-一次选择来源)）
- [x] run 详情返回真实 session 目录及全部 run-owned 记录；session 详情与 transcript 严格校验 run 归属。（[设计 §7.2](m3_transcript_history_implementation_design.md#72-run-详情与-session-完整过程)；[API §4.2](m3_transcript_history_api.md#42-run-详情)）
- [x] session transcript 全量有序，沿全部原记录的显式引用装载公共闭包并去重；不只挑最终总结。（[设计 §7.2](m3_transcript_history_implementation_design.md#72-run-详情与-session-完整过程)；[API §4.3](m3_transcript_history_api.md#43-agent-详情与完整-transcript)）
- [x] 实现统一全文 API，内联/外置可读，未知版本和损坏明确表示；安全出口和 history-only 类型规则统一。（[设计 §5.4](m3_transcript_history_implementation_design.md#54-未知版本与损坏)；[API §4.4](m3_transcript_history_api.md#44-全文)）
- [x] run 历史列表默认 50、最大 200、created_at/run_id 倒序，base64url 版本游标；无 total/filters/Agent 分页。（[设计 §7.4](m3_transcript_history_implementation_design.md#74-run-历史分页)；[API §4.1](m3_transcript_history_api.md#41-run-历史列表)）
- [x] 四个读取入口的 view、source、persistence、issues、URL 传递与取消可见性一致。（[设计 §7.1](m3_transcript_history_implementation_design.md#71-一次选择来源)；[设计 §7.3](m3_transcript_history_implementation_design.md#73-可见性)）
- [x] 补齐 create/cancel/retry 和错误 HTTP 映射，保留 request_id/error_id，参数无效 422。（[API §1](m3_transcript_history_api.md#1-基础约定与路由)；[API §7](m3_transcript_history_api.md#7-错误与排查字段)）
- [x] retry 的 200/202/409/404/503 正确；创建/手动补写/业务取消与连接断开不同义。（[API §6.2](m3_transcript_history_api.md#62-persistenceretry)）
- [x] 更新 OpenAPI、API 示例与前端约定；旧路径已删除，M2 文档仍作为旧阶段证据。（[设计 §2](m3_transcript_history_implementation_design.md#2-当前代码与目标模块)；[API §9](m3_transcript_history_api.md#9-排障与验证)）

### Tests

- [x] live 有而 PG 故障可读；strict history 不出现内存独有 item，auto fallback 保持取消限制。（[设计 §7.1](m3_transcript_history_implementation_design.md#71-一次选择来源)；[设计 §7.3](m3_transcript_history_implementation_design.md#73-可见性)）
- [x] 公共全文只读原 item、view 传递、跨 run/session/item 404，不用全局 item ID 绕过归属。（[API §4.4](m3_transcript_history_api.md#44-全文)）
- [x] 大型完整过程不截断、引用闭包有序去重且不扩张到其他 Agent 全部上下文。（[设计 §7.2](m3_transcript_history_implementation_design.md#72-run-详情与-session-完整过程)）
- [x] 配置损坏保留列表 item/issues，完整 DB 故障 503，不假空/假末页/假 404。（[设计 §5.4](m3_transcript_history_implementation_design.md#54-未知版本与损坏)；[API §7](m3_transcript_history_api.md#7-错误与排查字段)）
- [x] 同 created_at UUID tie-break、状态变化、页间新插入、最后一页、空库、游标精度及重启。（[设计 §7.4](m3_transcript_history_implementation_design.md#74-run-历史分页)）
- [x] 非法 view/limit/cursor/未知 query 参数，以及无 Agent 分页参数契约。（[API §1](m3_transcript_history_api.md#1-基础约定与路由)；[API §4.1](m3_transcript_history_api.md#41-run-历史列表)）
- [x] 取消和手动保存各分支，包括已释放历史的处理。（[API §6](m3_transcript_history_api.md#6-取消与手动保存)）

### Verify

- [x] `python -m pytest backend/tests/api backend/tests/application backend/tests/persistence -q`。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）
- [x] 导出实际 OpenAPI 对照本阶段路由及错误/响应模型，确认旧路由不存在；十条 M3 目标中的 session SSE 明确保留给 Milestone 7。（[API §1](m3_transcript_history_api.md#1-基础约定与路由)；[API §9](m3_transcript_history_api.md#9-排障与验证)）

### 审查回补（2026-09-14）

- [ ] 读取侧不再对序列化 JSON 整体执行 plain-text 正则；解析 JSON 后递归安全复核并按 registry 的规范格式重编码，无法解析时诚实返回 corrupt/unreadable。（[设计 §5.4](m3_transcript_history_implementation_design.md#54-未知版本与损坏)；[设计 §7.1](m3_transcript_history_implementation_design.md#71-一次选择来源)）
- [ ] create query 在 HTTP/应用输入边界拒绝 PostgreSQL TEXT 无法保存的 NUL 字符并返回稳定 4xx，不包装成可重试数据库 503。（[API §2](m3_transcript_history_api.md#2-创建与幂等)；[API §7](m3_transcript_history_api.md#7-错误与排查字段)）
- [ ] OpenAPI 表达 `Idempotency-Key` 的 UUID v4 和 `Last-Event-ID` 的 ASCII 十进制约束，同时保持既定错误响应语义。（[API §2](m3_transcript_history_api.md#2-创建与幂等)；[API §5.4](m3_transcript_history_api.md#54-游标与结束)）
- [ ] 增加含 `Authorization:` 字样的合法 JSON 在 list/detail/full-content 的往返测试、NUL 输入测试和 OpenAPI schema 断言，确保结构、字节数和错误分类一致。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）

阶段出口：历史可诚实读取全部已保存事实，当前与历史只有来源/可见性差异，没有重复实现。

#### Milestone 6 完成记录（2026-09-14）

- 代码状态：`codex/m3-milestone` 分支上的 Milestone 6 本地提交；保留此前 Milestone 0–5 本地提交，未推送远端。
- 实现位置：`application/transcripts/read_models.py` 与 `reader.py` 统一 live/PostgreSQL DTO、来源选择、全文和版本游标；生产 registry 同时承担写入与读取解析；`RunCoordinator` 在短临界区提供不可变 live 快照；memory/PostgreSQL store 实现相同读取 port，PostgreSQL 多语句读取使用 `REPEATABLE READ, READ ONLY`；`api/http/analysis_runs.py` 接入历史列表、run/session/transcript/全文、取消及手动补写，并由统一错误层输出安全关联信息。
- 读取边界：单次响应只选择 live 或 PostgreSQL；严格 history 不补入内存内容，auto 回退数据库后仍执行取消可见性规则；session 与全文均校验 run 归属，未知/损坏内容保留身份和 issue，外置正文缺失明确失败。
- 分页与契约：历史列表固定 created_at/run_id 倒序及版本化 base64url keyset cursor，默认 50、最大 200；非法 view/limit/cursor/未知参数为 422。实际 OpenAPI 为 10 个当前操作（9 个 analysis 操作加 `/health`），旧 `/api/analysis/sessions` 不存在；目标 session SSE 路由仍归 Milestone 7。
- 指定验证：`WATERAGENTS_TEST_POSTGRES_PORT=55462 ... backend/.venv/bin/python -m pytest backend/tests/api backend/tests/application backend/tests/persistence -q`：446 passed，真实 PostgreSQL 测试未跳过。
- 完整回归：同一隔离 PostgreSQL 下 `backend/.venv/bin/python -m pytest backend/tests -q`：754 passed；`python -m compileall -q backend/app backend/tests` 与 `git diff --check` 通过。
- 契约结论：live 故障隔离、历史一致性、完整过程/引用、全文归属、安全错误、游标稳定性与取消/补写各分支均有可执行测试；两层 SSE、消息生命周期、启动遗留修复与清理继续由 Milestone 7–8 承接。

## Milestone 7：两层 SSE、消息生命周期与续传

### 工作

- [x] run_snapshot 从原状态装配完整轻量通知，不持久化通知或复制完整消息。（[设计 §8.1](m3_transcript_history_implementation_design.md#81-run-通知)；[API §5.2](m3_transcript_history_api.md#52-run_snapshot)）
- [x] actual session 目录与水体参与状态按原事实生成；M3 无拓扑时不伪造，测试覆盖有拓扑情形。（[设计 §8.1](m3_transcript_history_implementation_design.md#81-run-通知)）
- [x] session process 先记录稳定整数 ID，再唤醒订阅者；无订阅也保留续传日志。（[设计 §8.2](m3_transcript_history_implementation_design.md#82-session-过程)）
- [x] 完成 transcript_item、message_started、message_delta、message_finalized 和 stream_end 编码。（[API §5.3](m3_transcript_history_api.md#53-session-过程与三个消息事件)；[API §5.4](m3_transcript_history_api.md#54-游标与结束)）
- [x] 消息开始固定原身份/顺序/时间，delta 只传新增安全文本，finalized 只更新原消息、从正文派生结束状态。（[设计 §3.1](m3_transcript_history_implementation_design.md#31-身份与顺序)；[设计 §8.2](m3_transcript_history_implementation_design.md#82-session-过程)）
- [x] 消息定稿不等 DB，不再生成同义 transcript item；大正文统一 preview/ref。（[设计 §8.2](m3_transcript_history_implementation_design.md#82-session-过程)；[设计 §5.3](m3_transcript_history_implementation_design.md#53-单份正文与大内容)）
- [x] 初订阅先全部已有后新增，重连补大于 cursor 的事件，锁内交接无缺口。（[设计 §8.3](m3_transcript_history_implementation_design.md#83-续传握手与结束)）
- [x] session Last-Event-ID ASCII 整数/0/缺省、400/409；run 通知不编号也不复用过程 cursor。（[API §5.4](m3_transcript_history_api.md#54-游标与结束)）
- [x] live 合法握手统一 200，已结束先补齐再 end；移除旧 204 行为与相关 smoke 断言。（[设计 §8.3](m3_transcript_history_implementation_design.md#83-续传握手与结束)；[API §5.4](m3_transcript_history_api.md#54-游标与结束)）
- [x] session 与 run 各自满足关闭条件后 stream_end；异常断开不发成功结束，keepalive 无编号。（[设计 §8.3](m3_transcript_history_implementation_design.md#83-续传握手与结束)；[API §5.1](m3_transcript_history_api.md#51-公共传输)）
- [x] live 释放的 410/history_url、完全不存在 404、DB 失败 503 和创建注册窗口收敛正确。（[设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)；[API §5.4](m3_transcript_history_api.md#54-游标与结束)）

### Tests

- [x] run 首次/重连只最新完整快照，状态变化不产生第二套 DB 通知。（[设计 §8.1](m3_transcript_history_implementation_design.md#81-run-通知)）
- [x] 无订阅已有内容、首次发送期间新增、多订阅者、重连及重复帧消费。（[设计 §8.3](m3_transcript_history_implementation_design.md#83-续传握手与结束)）
- [x] 三阶段顺序、同消息原 ID、跨消息交错、最终累计文本不重复拼接、大小正文表示。（[设计 §8.2](m3_transcript_history_implementation_design.md#82-session-过程)）
- [x] 较早生成时间保留，SSE sequence、lifecycle sequence、transcript_sequence 不混淆。（[设计 §3.1](m3_transcript_history_implementation_design.md#31-身份与顺序)；[设计 §8.2](m3_transcript_history_implementation_design.md#82-session-过程)）
- [x] session 结束不等待 run 保存；run 保存中/耗尽继续通知，最终全保存先快照再 end。（[设计 §8.3](m3_transcript_history_implementation_design.md#83-续传握手与结束)）
- [x] 200 空补齐后 end、400/409、410、意外 EOF、客户端关闭不取消执行。（[API §5.1](m3_transcript_history_api.md#51-公共传输)；[API §5.4](m3_transcript_history_api.md#54-游标与结束)）
- [x] 逐切分凭据推送安全与正常文本持续输出。（[设计 §9.2](m3_transcript_history_implementation_design.md#92-凭据边界)）

### Verify

- [x] `python -m pytest backend/tests/api backend/tests/application backend/tests/observability -q`。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）
- [x] 可执行 fixture 对照 API 中每个事件名、wrapper、id、结束与错误示例。（[API §5](m3_transcript_history_api.md#5-两层-sse)；[API §7](m3_transcript_history_api.md#7-错误与排查字段)）

### 审查回补（2026-09-14）

- [ ] 流式 assignment 在当前 chunk 只到 `key=`/尾随空白时保留 pending，等待下一字符后再决定 quoted/token 模式；引号位于下一 chunk 时秘密仍不得进入 SSE。（[设计 §9.2](m3_transcript_history_implementation_design.md#92-凭据边界)）
- [ ] final body 与已发送安全文本不一致时，以实际 `message.safe_text` 构造 `INCOMPLETE/ERROR` 原消息并追加诊断，不能用矛盾 body 覆盖已展示内容。（[设计 §8.2](m3_transcript_history_implementation_design.md#82-session-过程)）
- [ ] `message_started` 固定的 tags 与 `rag_eligible` 随 StreamingMessage、message_finalized、GET/history 和持久化 recheck 保持一致，不退回注册类型默认值。（[设计 §5.2](m3_transcript_history_implementation_design.md#52-消息与元信息)；[设计 §8.2](m3_transcript_history_implementation_design.md#82-session-过程)）
- [ ] 对 quoted assignment、header、URL 和显式 secret 的每个 chunk 分割点做参数化测试；增加 mismatch 与 metadata 的 started/finalized/stored 三层一致性测试。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）

阶段出口：原过程一份、live 事件可续传，数据库不按 token/frame 增长，实时查看不改变执行。

#### Milestone 7 完成记录（2026-09-14）

- 代码状态：`codex/m3-milestone` 分支、基线 `23f0745` 上的 Milestone 7 工作区改动；保留此前 Milestone 0–6 本地提交，本阶段尚未提交或推送。
- 实现位置：`application/transcripts/stream_models.py` 定义 run 快照、session 过程和消息三阶段模型；`RunCoordinator` 从原 run/session/水体/保存事实装配快照，在锁内先记录 session 过程序号再通知，并以原消息身份完成跨片段脱敏、增量与定稿；`api/sse` 分离 run/session 响应，HTTP 增加嵌套 session SSE 路由及 400/409/410 分类。
- 流与存储边界：run 重连只取得最新完整轻量快照且无编号；session 首订阅回放全部已有可见过程，重连仅补大于自身整数 cursor 的记录。控制通知、增量帧和定稿帧不新增数据库行，定稿消息只保存一份原正文；不一致正文另产生安全诊断而不静默改写已展示前缀。
- 关闭与错误：合法 live 握手统一 200，run/session 在各自条件满足后发送无编号 `stream_end`；keepalive 无编号，异常 EOF 与客户端关闭不伪装正常结束或取消执行。释放后返回 410/`live_state_gone` 和 `history_url`，不存在为 404，必要历史读取失败为 503。
- 指定验证：`backend/.venv/bin/python -m pytest -q backend/tests/api backend/tests/application backend/tests/observability`：447 passed；`python -m compileall -q backend/app backend/tests` 与 `git diff --check` 通过。
- 完整回归：macOS、Python 3.12.14、隔离 PostgreSQL 17.10 下，通过 `WATERAGENTS_TEST_POSTGRES_*` 运行 `backend/.venv/bin/python -m pytest -q backend/tests`：729 passed，无 skip。
- 可执行 smoke：同一隔离 PostgreSQL 完成迁移后运行 `backend/.venv/bin/python backend/scripts/run_sse_smoke.py`，输出 `{"completed":"passed","cancelled":"passed"}`；覆盖动态 loopback Uvicorn、完整快照、keepalive、重连、取消、最终保存快照、`stream_end` 与 EOF。
- 契约结论：M7 两层 SSE、过程续传、消息生命周期、凭据安全与 200/400/409/410/503 边界均有可执行证据；取消定稿/晚到审计、启动遗留修复和 live grace 清理继续由 Milestone 8 承接，三平台实际 CI 仍保持 partial。

## Milestone 8：取消、遗留修复、关闭与 live 清理

### 工作

- [x] run cancel 在同一边界关闭调度、固定生成中原消息、通知全部未终态 session；不做 HTTP 自调用。（[设计 §9.1](m3_transcript_history_implementation_design.md#91-原消息与晚到审计)；[API §6.1](m3_transcript_history_api.md#61-cancel)）
- [x] 晚到新增文本聚合为引用原消息的独立 history-only 审计，不复制前缀或另存拼接全文。（[设计 §9.1](m3_transcript_history_implementation_design.md#91-原消息与晚到审计)）
- [x] 取消跨片段凭据状态延续，原消息安全定稿在 session stream_end 前完成。（[设计 §9.1](m3_transcript_history_implementation_design.md#91-原消息与晚到审计)；[设计 §9.2](m3_transcript_history_implementation_design.md#92-凭据边界)）
- [x] late audit/清理事实计入必需写入及执行收敛，当前 GET/SSE/全文/引用/下游均不能泄露。（[设计 §7.3](m3_transcript_history_implementation_design.md#73-可见性)；[设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理)）
- [x] lifespan 连接→schema→遗留修复→接收流量；不自动 DDL，失败安全退出。（[设计 §10.3](m3_transcript_history_implementation_design.md#103-启动恢复和关闭)）
- [x] 按 run 逐事务修复 PG 非终态 run/session，包含终态 run 下的未终态 sessions。（[设计 §10.3](m3_transcript_history_implementation_design.md#103-启动恢复和关闭)）
- [x] 修复保持原身份/旧正文/原终态，新状态与 service_interrupted 记录同事务；重复启动不重复修复。（[设计 §10.3](m3_transcript_history_implementation_design.md#103-启动恢复和关闭)；[设计 §6.3](m3_transcript_history_implementation_design.md#63-原子提交超时与核实)）
- [x] stop 与用户 cancel 分开，关闭有界保存/协作收敛、writer 和 engine 资源退出；无自动 resume。（[设计 §10.3](m3_transcript_history_implementation_design.md#103-启动恢复和关闭)）
- [x] terminal + quiesced + 全部保存后起算完整 grace，新增待写失效计时，清理前锁内复查。（[设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理)）
- [x] cleanup 幂等/可重试，失败不改变终态；不增加慢 SSE 排空/ACK 机制。（[设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理)；[设计 §8.3](m3_transcript_history_implementation_design.md#83-续传握手与结束)）

### Tests

- [x] cancel 与 chunk/finalize/terminal 交错：前缀固定、尾部仅审计、已完成消息不降级。（[设计 §9.1](m3_transcript_history_implementation_design.md#91-原消息与晚到审计)）
- [x] audit 的同批引用/回滚/补写、auto PG fallback/direct fulltext/ref 不绕过。（[设计 §9.1](m3_transcript_history_implementation_design.md#91-原消息与晚到审计)；[设计 §7.3](m3_transcript_history_implementation_design.md#73-可见性)）
- [x] pending/running 的 run 与 session 各种组合，包括终态父/非终态子、重复启动和中途修复失败。（[设计 §10.3](m3_transcript_history_implementation_design.md#103-启动恢复和关闭)）
- [x] 原创建 receipt、旧序号时间、终态保持，新修复时间表达真实修复时刻。（[设计 §10.3](m3_transcript_history_implementation_design.md#103-启动恢复和关闭)；[设计 §3.3](m3_transcript_history_implementation_design.md#33-时间)）
- [x] 未收敛/未全保存/grace 未到均不清理；补写后完整窗口、cleanup 失败重试。（[设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理)）
- [x] 活动 SSE、在途 SQL 和未完成模型任务下的实际 shutdown 资源关闭。（[设计 §10.3](m3_transcript_history_implementation_design.md#103-启动恢复和关闭)）

### Verify

- [x] `python -m pytest backend/tests/application backend/tests/persistence backend/tests/api -q`。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）
- [x] 真实进程停止/重启后 history 仍可读，SSE 正确 410，遗留不恢复执行。（[设计 §10.3](m3_transcript_history_implementation_design.md#103-启动恢复和关闭)；[API §5.4](m3_transcript_history_api.md#54-游标与结束)）

### 审查回补（2026-09-14）

- [ ] run 进入 failed/cancelled 时在同一终态边界固定全部活动消息，并把所有未终态 session 分别转为 failed/cancelled；消息 finalization 必须先于对应 session 终态过程事件。（[设计 §3.2](m3_transcript_history_implementation_design.md#32-当前状态)；[设计 §9.1](m3_transcript_history_implementation_design.md#91-原消息与晚到审计)）
- [ ] workflow 正常返回前检查没有活动消息或未终态 session；违反内部 workflow 契约时按失败收束，不能发布 completed 后永久保留草稿。（[设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理)）
- [ ] 单独取消 session 时只固定该 session 的活动消息；run/session 取消后通用 reserve/finalize 在途结果必须抑制或转换为明确 history-only 审计，不能进入当前 GET/SSE/下游，也不能留下未定稿 reservation 造成 pending。（[设计 §7.3](m3_transcript_history_implementation_design.md#73-可见性)；[设计 §9.1](m3_transcript_history_implementation_design.md#91-原消息与晚到审计)）
- [ ] 故障与竞态测试覆盖 workflow 抛错、正常返回但遗留草稿、run cancel 后 generic finalize、单 session cancel 后继续 delta/finalize；最终必须 session/run SSE 结束、partial 正文可保存、persistence 可收敛且 live state 可清理。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）

阶段出口：中断不会伪造完成、保存或恢复；正常结束后可释放资源而保持全文历史。

#### Milestone 8 完成记录（2026-09-14）

- 取消边界：`RunCoordinator` 在选定 cancelled 的同一临界区安全定稿所有生成中消息，保留已展示前缀；之后到达的片段继续经过跨片段凭据过滤，只形成引用原 item 的 history-only `LateMessageOutputFact`，不再进入 live GET/SSE 或下游上下文。
- 启动与修复：正式 lifespan 严格执行数据库连接、Alembic head 检查、逐 run 原子修复，再创建 coordinator 接收流量。遗留 pending/running run/session 记为 `failed/service_interrupted`，包括终态父 run 下的活动子 session；保留原身份、正文、回执和既有终态，重复启动不重复追加。
- 清理与关闭：live state 仅在业务终态、workflow quiesced、晚到消息收敛、全部必需写入确认后开始完整 grace；条件变化使计时失效，锁内复查后释放，失败重新等待并重试。服务 stop 与用户 cancel 分离，关闭收敛 workflow、writer 和 engine，不恢复 Agent。
- 验证：取消/脱敏、失败中途修复、重复启动、终态父/活动子、cleanup 失败重试均进入完整 `735 passed` 回归；真实 PostgreSQL 重启 smoke 进一步验证 history 可读、SSE 410 和不重跑 workflow。

## Milestone 9：完整集成、三平台 smoke 与运行手册

### 工作

- [x] 更新 health smoke 以满足正式 PG 启动门槛；新增/替换 run_history_smoke，覆盖两层流与历史。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)；[API §9](m3_transcript_history_api.md#9-排障与验证)）
- [x] smoke 使用真实 Uvicorn 与 PG，动态 loopback 端口、禁用环境代理、确定性 gate，不用真实 LLM。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)；[API §9](m3_transcript_history_api.md#9-排障与验证)）
- [x] 按 API 指南通过 WATERAGENTS_TEST_DB_NAME 指定不同于业务库的测试数据库；fixture/smoke 的临时 schema、Alembic version 表和子进程一致，缺连接明确失败且清理不碰业务库。（[API §9](m3_transcript_history_api.md#9-排障与验证)）
- [x] 完成 CI 三平台单元/协议与实际 PG smoke 方案；平台不支持 service containers 时使用可用 PG 供应方式。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）
- [x] 运行从空环境配置到 upgrade/check/start，再 stop/restart/history 的完整文档步骤。（[API §8.2](m3_transcript_history_api.md#82-本地-compose)；[API §8.4](m3_transcript_history_api.md#84-停止重启与检查)）
- [x] 验证远程实例配置、SSL、特殊字符密码和版本不匹配错误，不把仅本机 Compose 成功当作跨环境完成。（[API §8.1](m3_transcript_history_api.md#81-环境变量)；[API §8.3](m3_transcript_history_api.md#83-已有数据库与升级)）
- [ ] 按 API §8.5 完成跨系统切换：三个 OS 分别作为目标，复用同版源码/锁及一份已保存测试数据，停止旧实例后再启动目标；记录实际 OS/CPU/Python 组合。（[设计 §10.4](m3_transcript_history_implementation_design.md#104-跨操作系统可迁移性)；[API §8.5](m3_transcript_history_api.md#85-更换操作系统或机器的最短流程)）
- [x] 验证接回原 PG 无需改表/搬历史；新空 PG 只用现有 Alembic 初始化同一 schema，不另建按 OS 分叉的迁移或数据库复制工具。（[API §8.5](m3_transcript_history_api.md#85-更换操作系统或机器的最短流程)）
- [x] 完成 OpenAPI、事件文档、README、配置示例、测试命令与真实文件路径一致性检查。（[API §9](m3_transcript_history_api.md#9-排障与验证)；[前端约定 §7](../frontend_integration_contracts.md#7-同步规则)）
- [x] 普通 query 可用于诊断，日志不暴露 secret；保存 warning 可通过 run/session/request/error ID 追溯。（[设计 §9.2](m3_transcript_history_implementation_design.md#92-凭据边界)；[API §7](m3_transcript_history_api.md#7-错误与排查字段)）

### Verify

- [x] `python -m pytest backend/tests -q`。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)；[API §9](m3_transcript_history_api.md#9-排障与验证)）
- [x] `python -m pytest backend/tests/persistence -q`（真实 PG）。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)；[API §9](m3_transcript_history_api.md#9-排障与验证)）
- [x] `python backend/scripts/health_smoke.py`。（[API §9](m3_transcript_history_api.md#9-排障与验证)）
- [x] `python backend/scripts/run_history_smoke.py`。（[API §9](m3_transcript_history_api.md#9-排障与验证)）
- [ ] Ubuntu/macOS/Windows + Python 3.11 的实际结果与退出资源证据。（[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）
- [ ] 迁移前后对照原 run/session/item ID、UTC 时间、顺序、历史 cursor、同 key 原回执及大正文全文；确认没有重跑分析或依赖旧机器本地路径。（[设计 §10.4](m3_transcript_history_implementation_design.md#104-跨操作系统可迁移性)；[API §8.5](m3_transcript_history_api.md#85-更换操作系统或机器的最短流程)）
- [x] `git diff --check`、相对文档链接/锚点、配置引用和协议示例检查。（[前端约定 §7](../frontend_integration_contracts.md#7-同步规则)；[证据记录格式](#实际证据记录格式)）

### 审查回补（2026-09-14）

- [ ] `run_history_smoke.py` 通过两个独立 OS 子进程启动正式 Uvicorn/app 入口，完整停止第一进程后再以同一数据库启动第二进程；不能以同一解释器内重建 ASGI app 冒充 process restart。（[设计 §10.4](m3_transcript_history_implementation_design.md#104-跨操作系统可迁移性)；[设计 §11](m3_transcript_history_implementation_design.md#11-测试与验收设计)）
- [ ] 子进程停止使用跨平台 terminate、超时与 kill fallback，验证 fresh import、环境重载、退出资源和重启修复；health/history smoke 均不得依赖 Bash、GNU 工具或旧进程模块状态。（[设计 §10.4](m3_transcript_history_implementation_design.md#104-跨操作系统可迁移性)）
- [ ] README 统一命令执行目录；标准 venv 路径不依赖未安装的 `uv`，启动、数据库命令和 smoke 的项目/脚本路径均可直接复制执行。（[API §8](m3_transcript_history_api.md#8-跨平台搭建与配置)；[API §9](m3_transcript_history_api.md#9-排障与验证)）
- [ ] 在带合法 `backend/.env` 的本地环境重新运行完整测试、真实 PostgreSQL 测试、health smoke 和独立进程 history smoke，并记录真实结果。（[证据记录格式](#实际证据记录格式)）

阶段出口：记录真实命令、平台/版本、通过/失败数量及 CI 入口。任何平台缺失保持 partial，不借用 M2 测试数量。

#### Milestone 9 本机完成记录（2026-09-14）

- 实现：正式启动增加严格 schema 门禁与启动修复；health smoke 走正式 `python -m app`；新增真实 Uvicorn/PostgreSQL 的 `run_history_smoke.py`；数据库集成与 smoke 统一由 `WATERAGENTS_TEST_DB_NAME` 指向专用测试库，并为每组创建、迁移、检查和删除唯一临时 schema。
- smoke 覆盖：完成/取消/遗留三类 run、两层 SSE 消息生命周期、UTF-8 大正文全文、历史分页与顺序、停止后重启修复、live 410/history_url、同 key 原回执，以及修复后不重新执行 workflow。输出分别为 `{"status":"healthy"}` 与 `{"history":"passed","restart_repair":"passed","database":"wateragents_m9"}`。
- 本机环境与结果：macOS 26.6.2 arm64、Python 3.12.14、PostgreSQL 17.10、Alembic revision `20260913_01`；完整 `backend/tests` 为 735 passed，真实 PostgreSQL persistence 为 39 passed，compileall 和 focused 跨平台/取消测试通过。
- CI 入口：`.github/workflows/backend-m1.yml` 使用 Ubuntu/macOS/Windows、Python 3.11 和 PostgreSQL 17，逐平台执行完整测试、health smoke 与 run/history smoke；workflow 方案已交付。
- partial：本次未取得三个远端 runner 的实际链接/结果，也未把一份已保存数据库依次接到三个目标 OS。因此“跨系统切换”和两项对应 Verify 保持未勾选；本机停止/重启对照已覆盖同库身份、游标、回执、全文与不重跑分析。

## Milestone 10：契约验收与后续交接

### 工作

- [x] 为下表每条 contract 填写实现文件、测试位置、实际命令/结果和剩余风险；不只填写“已支持”。（[契约覆盖矩阵](#契约覆盖矩阵)；[证据记录格式](#实际证据记录格式)）
- [x] 核对 GLOBAL 的模型/时间/事件/日志/跨平台规则，以及当前适用 MR-G01–MR-G14 的交付要求。（[契约 §2](../deferred_milestone_contracts.md#2-跨-milestone-硬约束)；[契约 §4](../deferred_milestone_contracts.md#4-后续-mr-合规门禁)）
- [x] 更新中心承接登记：全部必要实现和证据满足后才标已承接；未完成项给出明确后续位置。（[契约 §13](../deferred_milestone_contracts.md#13-承接记录)）
- [x] 登记 M4 接入真实 parser session、AnalysisContext/BFS 和原类型注册的边界，不改四表事实原则。（[设计 §1.2](m3_transcript_history_implementation_design.md#12-后续阶段承接)；[契约 §6](../deferred_milestone_contracts.md#6-m4query-understanding--hydrology-topology)）
- [x] 登记 M6 接入真实消息/工具/skill/hook/结果、适用置信度和综合水质指标的边界；M3 fixture 不成为业务 schema。（[设计 §1.2](m3_transcript_history_implementation_design.md#12-后续阶段承接)；[契约 §8](../deferred_milestone_contracts.md#8-m6single-agent-reasoning)）
- [x] 登记 M7 接入 DAG、上游失败策略与水体参与状态的边界；引用索引仅按真实需要评估。（[设计 §1.2](m3_transcript_history_implementation_design.md#12-后续阶段承接)；[契约 §9](../deferred_milestone_contracts.md#9-m7topology-driven-multi-agent-workflow)）
- [x] 登记 M8 接入显式跨 run 来源与 RAG 的边界，保持 stateless 和当前上下文边界。（[设计 §1.2](m3_transcript_history_implementation_design.md#12-后续阶段承接)；[契约 §10](../deferred_milestone_contracts.md#10-m8memoryrag-from-transcript)）
- [x] 登记产品阶段 M9 的地图/连线、多层完整阅读、两层 SSE、保存按钮与历史管理边界；取消颜色仍待前端设计。（[前端约定 §5](../frontend_integration_contracts.md#5-m4m9-的前端接力边界)；[契约 §11](../deferred_milestone_contracts.md#11-m9settingskeysadmin--demo-closure)）
- [x] 在本清单追加最终完成记录，说明改动、验证、限制和可复现入口。（[设计 §12](m3_transcript_history_implementation_design.md#12-完成标准与接力)；[证据记录格式](#实际证据记录格式)）

### 审查回补（2026-09-14）

- [ ] 以运行时实现和实际 OpenAPI 为依据统一公开错误码，移除或更正 `persistence_write_timeout`、`unsupported_content`、`persistence_identity_conflict` 等漂移名称。（[API §7](m3_transcript_history_api.md#7-错误与排查字段)）
- [ ] 前端约定明确 `completed_with_persistence_error` 只属于 run，session 保持五态；run/session terminal event 与保存 warning 的职责不混用。（[前端约定 §4.5](../frontend_integration_contracts.md#45-持久化异常与取消后的内容)）
- [ ] 修正完整 item 示例的 `content_bytes`，并以正式 registry 规范序列化生成的可执行 golden 覆盖示例字节数、时间格式和事件 wrapper。（[API §3.4](m3_transcript_history_api.md#34-完整-item-示例)；[API §5](m3_transcript_history_api.md#5-两层-sse)）
- [ ] Milestone 2–9 回补全部完成后重新审计 M3-C01–C20、M3-A01–A12、GLOBAL/MR 门禁，更新本清单、实施设计、API 指南、前端约定和中心承接状态；未取得的三平台证据继续保持 partial。（[契约覆盖矩阵](#契约覆盖矩阵)；[契约 §13](../deferred_milestone_contracts.md#13-承接记录)）

### 契约覆盖矩阵

下表逐项覆盖全部 M3 contract ID；同一行只合并共享实现边界与验收用例的条目。`本机已验证` 表示当前代码、focused/regression、真实 PostgreSQL 与正式进程 smoke 均有证据，`回补待验证` 表示首轮曾通过但本次审查已发现会破坏该保证的缺口，必须完成所属 Milestone 回补并重新取得证据。两者都不等于中心最终 `implemented`；所有行仍共同等待 GLOBAL-C11 的三平台实际结果。

| 契约 | 状态 | 实现位置 | 重点测试位置 | 文档与剩余风险 |
|---|---|---|---|---|
| GLOBAL-C11 | partial | `backend/app/infrastructure/config/`、`runtime/event_loop.py`、`scripts/test_database.py`、两条 smoke、三平台 workflow | `test_cross_platform_ci.py`、`test_database_config.py`、两条 smoke | [设计 §10.4](m3_transcript_history_implementation_design.md#104-跨操作系统可迁移性)；缺 Ubuntu/macOS/Windows 远端实际 CI 与同库依次切换证据。 |
| M3-C01 / M3-A01 | 本机已验证 | `application/transcripts/buffer.py`、`analysis_runs/persistence.py`、`infrastructure/persistence/schema.py`、`store.py` | `test_transcript_buffer.py`、`test_run_writer.py`、`test_postgres_persistence.py` | [设计 §3.1](m3_transcript_history_implementation_design.md#31-身份与顺序)；四表、live buffer、append-only item 与单份正文均有真实 PG 证据。 |
| M3-C02 / M3-A02 | 回补待验证 | `application/transcripts/models.py`、`registry.py`、`reader.py` | `test_transcript_registry.py`、`test_run_models.py`、`test_analysis_history.py` | [设计 §5](m3_transcript_history_implementation_design.md#5-typed-content-与读取表示)；读取侧结构化安全复核完成后重验。 |
| M3-C03 / M3-C07 | 回补待验证 | `application/analysis_runs/models.py`、`coordinator.py`、`api/schemas/analysis_runs.py`、`application/transcripts/reader.py` | `test_agent_session_models.py`、`test_agent_session_runtime.py`、`test_analysis_history.py` | [设计 §3.2](m3_transcript_history_implementation_design.md#32-当前状态)；workflow 结束时的父子终态收束完成后重验。 |
| M3-C04 | 回补待验证 | `api/http/analysis_runs.py`、`api/schemas/analysis_runs.py`、`application/transcripts/reader.py` | `test_analysis_history.py`、`test_analysis_runs.py`、`test_analysis_events.py` | [API §4](m3_transcript_history_api.md#4-get-读取接口)；读取安全与 OpenAPI header 约束完成后重验。 |
| M3-C05 / M3-C06 / M3-A03 | 回补待验证 | `application/transcripts/buffer.py`、`streaming.py`、`redaction.py`、`reader.py` | `test_transcript_buffer.py`、`test_streaming_redaction.py`、`test_analysis_history.py`、`test_json_logging.py` | [设计 §5.3](m3_transcript_history_implementation_design.md#53-单份正文与大内容)、[设计 §9.2](m3_transcript_history_implementation_design.md#92-凭据边界)；跨 chunk 泄漏与业务正文误杀修复后重验。 |
| M3-C08 | 回补待验证 | `application/analysis_runs/models.py`、`coordinator.py`、`creation.py`、`startup_repair.py` | `test_run_state_machine.py`、`test_run_creation.py`、`test_milestone8_runtime.py` | [设计 §6.1](m3_transcript_history_implementation_design.md#61-创建与所有权)；workflow 失败后的 run/session 收束完成后重验。 |
| M3-C09 / M3-A04 | 回补待验证 | `application/analysis_runs/creation.py`、`coordinator.py`、`infrastructure/persistence/store.py`、创建路由 | `test_run_creation.py`、`test_analysis_runs.py`、`test_postgres_persistence.py`、run/history smoke | [API §2](m3_transcript_history_api.md#2-创建与幂等)；非超时提交未知与外置正文顺序核实完成后重验。 |
| M3-C10 / M3-A05 | 回补待验证 | `application/analysis_runs/coordinator.py`、`application/transcripts/streaming.py`、`registry.py` | `test_milestone8_runtime.py`、`test_streaming_redaction.py`、`test_analysis_history.py` | [设计 §9.1](m3_transcript_history_implementation_design.md#91-原消息与晚到审计)；通用结果和单 session 取消边界完成后重验。 |
| M3-C11 / M3-A07 | 回补待验证 | `application/analysis_runs/coordinator.py`、`persistence.py`、`application/transcripts/reader.py` | `test_run_writer.py`、`test_analysis_history.py`、`test_analysis_events.py` | [设计 §6.4](m3_transcript_history_implementation_design.md#64-重试和交付窗口)；活动草稿、writer 退出与保存收敛修复后重验。 |
| M3-C12 / M3-C13 / M3-A06 | 回补待验证 | `application/analysis_runs/coordinator.py`、`runtime.py`、`main.py` | `test_milestone8_runtime.py`、`test_run_workflow.py`、`test_run_writer.py`、两条 smoke | [设计 §6.5](m3_transcript_history_implementation_design.md#65-保存状态手动恢复与清理)；失败 quiescence、SSE 结束和 live 清理完成后重验。 |
| M3-C14 | 回补待验证 | `main.py`、`application/analysis_runs/startup_repair.py`、`infrastructure/persistence/database.py`、`store.py` | `test_database_config.py`、`test_milestone8_runtime.py`、`test_postgres_persistence.py`、run/history smoke | [设计 §10.3](m3_transcript_history_implementation_design.md#103-启动恢复和关闭)；独立 OS 进程 restart smoke 完成后重验。 |
| M3-C15 / M3-A08 | 回补待验证 | `application/analysis_runs/coordinator.py`、`api/sse/encoder.py`、`response.py`、两层 SSE 路由 | `test_analysis_events.py`、`test_analysis_runs.py`、`test_run_sse_smoke.py`、run/history smoke | [API §5](m3_transcript_history_api.md#5-两层-sse)；消息一致性、失败关闭和 chunk 安全完成后重验。 |
| M3-C16 / M3-A09 | 回补待验证 | `application/analysis_runs/coordinator.py`、`application/transcripts/registry.py`、`reader.py` | `test_agent_session_runtime.py`、`test_transcript_registry.py`、`test_analysis_history.py` | [设计 §4.5](m3_transcript_history_implementation_design.md#45-索引引用及事务边界)；引用入口与消息 metadata 单源完成后重验。 |
| M3-C17 | 回补待验证 | `application/transcripts/reader.py`、统一详情/过程/全文路由 | `test_analysis_history.py`、`test_analysis_events.py` | [设计 §7.1](m3_transcript_history_implementation_design.md#71-一次选择来源)；读取 JSON 和取消可见性修复后重验。 |
| M3-C18 / M3-A10 | 回补待验证 | `application/analysis_runs/persistence.py`、`coordinator.py`、`infrastructure/persistence/store.py`、`records.py` | `test_run_writer.py`、`test_postgres_persistence.py`、`test_records.py` | [设计 §6.2](m3_transcript_history_implementation_design.md#62-runwriter)；session terminal 核实及 memory/PG parity 完成后重验。 |
| M3-C19 / M3-A11 | 回补待验证 | `infrastructure/observability/redaction.py`、`json_logging.py`、`application/transcripts/streaming.py` | `test_json_logging.py`、`test_log_correlation.py`、`test_streaming_redaction.py` | [设计 §9.2](m3_transcript_history_implementation_design.md#92-凭据边界)；流式泄漏、普通文本误杀和 warning 日志关联完成后重验。 |
| M3-C20 / M3-A12 | 回补待验证 | `application/analysis_runs/coordinator.py`、`persistence.py`、补写路由 | `test_run_writer.py`、`test_analysis_history.py`、`test_analysis_runs.py` | [API §6.2](m3_transcript_history_api.md#62-persistenceretry)；writer 异常与手动恢复不再形成无人负责的 pending 后重验。 |

### GLOBAL / MR 门禁审计

本次逐项复核 `GLOBAL-C01`–`GLOBAL-C11`、M2 保留基线、`M3-C01`–`M3-C20`、`M3-A01`–`M3-A12` 以及当前适用的 `MR-G01`–`MR-G14`。没有新增、拆分、替换或取消 contract，也没有把本机证据冒充远端 MR/CI 结果；本节只为后续提交 MR 提供可复核入口。

| 门禁范围 | 结论 | 证据或限制 |
|---|---|---|
| GLOBAL-C01–C03 | 满足本阶段适用范围 | 新后端、run/session/COMID 身份与当前 DTO 均由 production 路径、OpenAPI 和契约测试约束；未读取或兼容归档实现，也未引入通用 Waterbody。 |
| GLOBAL-C04、C09 | 本机已验证 | UTC/IANA 时间事实、typed DTO 与同源边界模型由 domain/application 原事实装配，不维护语义不同的重复状态。 |
| GLOBAL-C05–C06 | 保留并已登记接力 | M3 保存 UTC 时间、typed facts 与原引用；真实日期窗口和水质 evidence 闭包由 M4/M6 按下方入口验收，不用 fixture 冒充。 |
| GLOBAL-C07 | 本机已验证 | 写前、持久化出口、SSE、日志与异常诊断共同执行凭据脱敏，focused 与完整回归均覆盖。 |
| GLOBAL-C08 | 保留并已登记接力 | 当前后端契约已冻结；最终前端仍由产品 M9 按届时设计、OpenAPI 和 SSE 契约独立实现，不复用归档前端。 |
| GLOBAL-C10 | 已登记接力 | 下方 M4/M6/M7/M8/产品 M9 表给出入口、保留约束和可观察验收。 |
| GLOBAL-C11 | partial | 同一代码/锁/config/schema 与三平台 workflow 已交付；缺三平台远端实跑及同库依次切换证据。 |
| MR-G01–G02 | 证据就绪 | 清单包含 contract 范围、实现/测试位置、实际命令和未完成项；最终 MR 仍需引用真实 CI。 |
| MR-G03–G10 | 本机满足 | 设计、API、OpenAPI、fixture、smoke 同步；真实 PG 覆盖并发/回滚/恢复，安全、范围和竞态有专项用例。 |
| MR-G11–G12 | partial | 本机回归与 smoke 通过，但三平台实际 CI 未取得；因此中心最终表不写 `implemented`。 |
| MR-G13–G14 | 满足 | 未引入兼容层、第二套状态/正文或与明确验收无关的抽象；未新增 deferred contract。 |

### 后续阶段交接

| 阶段 | 接入边界 | 可观察验收 |
|---|---|---|
| M4 | parser 使用独立无 COMID session，通过现有 coordinator/registry 写入真实请求、过程与结构化 `AnalysisContext`；BFS/水体范围写原 run 事实，不改变四表和引用原则。 | 历史与 SSE 可读 parser 过程；只在水体 Agent 准备执行时创建其 session，未执行节点不预建。 |
| M6 | 通过现有 `operation_id`、writer、registry/reader 接入真实消息、模型、工具、skill、hook、结果、适用 confidence/provenance。 | 同名多次调用仍按身份配对，原 tool_call_id 往返保留；证据引用可追溯且不把 M3 fixture 变成业务 schema。 |
| M7 | DAG、上游 gate、取消传播和水体参与事实在原 run/session 边界接入；引用索引只在有实际查询需求时从原引用派生。 | 先满足上游门禁再创建/运行 session；快照与详情由同一事实装配，不存地图状态副本。 |
| M8 | 跨 run RAG 使用显式历史来源结构，保留原 run/item/provenance；普通 transcript 引用继续限于同一 run，运行仍 stateless。 | 重启后只读取已保存历史，不恢复 Agent 或未保存内存；来源身份和置信度可解释。 |
| 产品 M9 | 消费最终 OpenAPI、两层 SSE、统一 history/auto 读取、取消与补写；地图、连线、折叠阅读和取消颜色由前端产品设计完成。 | 合法 live 流 200 并以 `stream_end` 结束，释放后 410 转历史；分页/全文/公共引用可读，断开连接不取消 run。 |

### Milestone 10 完成记录（2026-09-14）

- 代码状态：开始时位于 `codex/m3-milestone`，工作区无未提交改动，基线为 `bac4edfcb9615e23c167627f509fa04f5b509d71`（`feat: complete M3 milestones 8 and 9`）。契约审计未发现需要新增 production code、测试专用 schema 或兼容层的缺口；本阶段只同步本清单、实施设计、API/runbook、前端接入约定和中心契约登记。
- 契约结论：矩阵完整覆盖 20 个 M3-C 和 12 个 M3-A ID，并复核 GLOBAL-C01–C11、M2 保留基线及 MR-G01–G14；未新增、拆分、替换或取消 contract。M4/M6/M7/M8/产品 M9 已有明确实现入口、保留约束和可观察验收。
- 本机环境：macOS 26.6.2 arm64、Python 3.12.14、PostgreSQL 17.10、SQLAlchemy 2.0.52、psycopg 3.3.5、Alembic 1.20.0，schema head `20260913_01`。临时实例监听 `127.0.0.1:55440`，专用测试库为 `wateragents_m10`；业务库名保护值为 `wateragents_business_guard`。
- 实际验证：在相同 `WATERAGENTS_DB_*` 与 `WATERAGENTS_TEST_DB_NAME=wateragents_m10` 环境下，`python -m pytest backend/tests -q` 为 735 passed；`python -m pytest backend/tests/persistence -q` 为 39 passed；代理污染环境下 `python backend/scripts/health_smoke.py` 输出 `{"status":"healthy"}`，`python backend/scripts/run_history_smoke.py` 输出 `{"history":"passed","restart_repair":"passed","database":"wateragents_m10"}`。`python -m compileall -q backend/app backend/scripts`、`git diff --check`、5 份同步文档的 529 个相对文件链接检查通过；中心 M3 集合与矩阵均为 32 个 ID，差集为空。
- 限制：GLOBAL-C11 仍缺 Ubuntu/macOS/Windows 远端 runner 的实际完整 tests/两项 smoke 结果，以及三系统依次接入同一已保存 PostgreSQL 的身份、游标、回执和全文引用核对。对应两个成功标准、Milestone 2/9 Verify 与中心最终承接保持未完成/`partial`；本机结果不替代这些证据。
- 可复现入口：[API §9](m3_transcript_history_api.md#9-排障与验证)列出完整测试与 smoke 命令，[API §8.5](m3_transcript_history_api.md#85-更换操作系统或机器的最短流程)列出三平台迁移步骤；中心状态见 [M3 待验收索引](../deferred_milestone_contracts.md#m3-待验收索引)。本阶段未提交、未推送、未创建 MR。

### 实际证据记录格式

每个阶段完成后补充以下信息，不提前填通过值：

| 字段 | 待记录内容 |
|---|---|
| 日期与代码状态 | 分支/提交或可复核 diff，已有改动保护情况。 |
| 实现位置 | 生产代码、migration、配置、脚本和相应测试。 |
| 环境 | OS、Python、PG minor/digest、依赖版本、schema revision。 |
| 验证 | 实际命令、结果、测试数量及 CI 链接（确实执行后填写）。 |
| 契约结论 | 已满足、partial 或未满足，缺少的具体证据。 |
| 后续接力 | 所属 Milestone/contract、入口、未实现业务边界。 |

Milestone 0–10 的首轮本机实现、契约审计与适用验证记录已保留；当前还必须完成各阶段的 2026-09-14 审查回补并重新验证。Milestone 2/9 的三平台实际 CI 和跨系统迁移证据同样仍为 partial，不能由本机结果替代。中心最终承接须在回补和外部证据均齐备后更新。
